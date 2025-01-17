from contextvars import ContextVar
from typing import List, Optional, Union, Dict
from enum import Enum
from pydantic import BaseModel, Field
import asyncio

from deepeval.test_case import (
    LLMTestCase,
    LLMTestCaseParams,
    ConversationalTestCase,
)
from deepeval.metrics import BaseMetric
from deepeval.models import DeepEvalBaseLLM
from deepeval.utils import (
    get_or_create_event_loop,
    generate_uuid,
    prettify_list,
)
from deepeval.metrics.utils import (
    print_intermediate_steps,
    validate_conversational_test_case,
    trimAndLoadJson,
    check_llm_test_case_params,
    initialize_model,
)
from deepeval.metrics.summarization.template import SummarizationTemplate
from deepeval.metrics.faithfulness.template import FaithfulnessTemplate
from deepeval.metrics.indicator import metric_progress_indicator

required_params: List[LLMTestCaseParams] = [
    LLMTestCaseParams.INPUT,
    LLMTestCaseParams.ACTUAL_OUTPUT,
]


class SummarizationAlignmentVerdict(BaseModel):
    # yes, no, or idk
    verdict: str
    reason: str = Field(default=None)


class SummarizationCoverageVerdict(BaseModel):
    summary_verdict: str
    original_verdict: str
    question: str = Field(default=None)


class ScoreType(Enum):
    ALIGNMENT = "Alignment"
    COVERAGE = "Coverage"


class SummarizationMetric(BaseMetric):

    @property
    def truths(self) -> Optional[List[str]]:
        return self._truths.get()

    @truths.setter
    def truths(self, value: Optional[List[str]]):
        self._truths.set(value)

    @property
    def claims(self) -> Optional[List[str]]:
        return self._claims.get()

    @claims.setter
    def claims(self, value: Optional[List[str]]):
        self._claims.set(value)

    @property
    def coverage_verdicts(self) -> Optional[List[SummarizationCoverageVerdict]]:
        return self._coverage_verdicts.get()

    @coverage_verdicts.setter
    def coverage_verdicts(
        self, value: Optional[List[SummarizationCoverageVerdict]]
    ):
        self._coverage_verdicts.set(value)

    @property
    def alignment_verdicts(
        self,
    ) -> Optional[List[SummarizationAlignmentVerdict]]:
        return self._alignment_verdicts.get()

    @alignment_verdicts.setter
    def alignment_verdicts(
        self, value: Optional[List[SummarizationAlignmentVerdict]]
    ):
        self._alignment_verdicts.set(value)

    @property
    def coverage_score(self) -> Optional[float]:
        return self._coverage_score.get()

    @coverage_score.setter
    def coverage_score(self, value: Optional[float]):
        self._coverage_score.set(value)

    @property
    def alignment_score(self) -> Optional[float]:
        return self._alignment_score.get()

    @alignment_score.setter
    def alignment_score(self, value: Optional[float]):
        self._alignment_score.set(value)

    def __init__(
        self,
        threshold: float = 0.5,
        n: int = 5,
        model: Optional[Union[str, DeepEvalBaseLLM]] = None,
        assessment_questions: Optional[List[str]] = None,
        include_reason: bool = True,
        async_mode=True,
        strict_mode: bool = False,
        verbose_mode: bool = False,
    ):
        super().__init__()
        self._truths: ContextVar[Optional[List[str]]] = ContextVar(
            generate_uuid(), default=None
        )
        self._claims: ContextVar[Optional[List[str]]] = ContextVar(
            generate_uuid(), default=None
        )
        self._coverage_verdicts: ContextVar[
            Optional[List[SummarizationCoverageVerdict]]
        ] = ContextVar(generate_uuid(), default=None)
        self._alignment_verdicts: ContextVar[
            Optional[List[SummarizationAlignmentVerdict]]
        ] = ContextVar(generate_uuid(), default=None)
        self._coverage_score: ContextVar[Optional[float]] = ContextVar(
            generate_uuid(), default=None
        )
        self._alignment_score: ContextVar[Optional[float]] = ContextVar(
            generate_uuid(), default=None
        )
        self.threshold = 1 if strict_mode else threshold
        self.model, self.using_native_model = initialize_model(model)
        self.evaluation_model = self.model.get_model_name()

        if assessment_questions is not None and len(assessment_questions) == 0:
            self.assessment_questions = None
        else:
            self.assessment_questions = assessment_questions

        self.include_reason = include_reason
        self.n = n
        self.strict_mode = strict_mode
        self.async_mode = async_mode
        self.verbose_mode = verbose_mode

    def measure(
        self,
        test_case: Union[LLMTestCase, ConversationalTestCase],
    ) -> float:
        if isinstance(test_case, ConversationalTestCase):
            test_case = validate_conversational_test_case(test_case, self)
        check_llm_test_case_params(test_case, required_params, self)

        self.evaluation_cost = 0 if self.using_native_model else None
        with metric_progress_indicator(self):
            if self.async_mode:
                loop = get_or_create_event_loop()
                (
                    self.truths,
                    self.claims,
                    self.coverage_verdicts,
                    self.alignment_verdicts,
                    self.coverage_score,
                    self.alignment_score,
                    self.score_breakdown,
                    self.score,
                    self.reason,
                    self.success,
                ) = loop.run_until_complete(self._measure_async(test_case))
            else:
                self.truths: List[str] = self._generate_claims(test_case.input)
                self.claims: List[str] = self._generate_claims(
                    test_case.actual_output
                )
                self.coverage_verdicts: List[SummarizationCoverageVerdict] = (
                    self._generate_coverage_verdicts(test_case)
                )
                self.alignment_verdicts: List[SummarizationAlignmentVerdict] = (
                    self._generate_alignment_verdicts()
                )
                self.alignment_score = self._calculate_score(
                    ScoreType.ALIGNMENT
                )
                self.coverage_score = self._calculate_score(ScoreType.COVERAGE)
                self.score_breakdown = {
                    ScoreType.ALIGNMENT.value: self.alignment_score,
                    ScoreType.COVERAGE.value: self.coverage_score,
                }
                self.score = min(self.alignment_score, self.coverage_score)
                self.reason = self._generate_reason()
                self.success = self.score >= self.threshold
                if self.verbose_mode:
                    print_intermediate_steps(
                        self.__name__,
                        steps=[
                            f"Truths:\n{prettify_list(self.truths)}\n",
                            f"Claims:\n{prettify_list(self.claims)}\n",
                            f"Assessment Questions:\n{prettify_list(self.assessment_questions)}\n",
                            f"Coverage Verdicts:\n{prettify_list(self.coverage_verdicts)}\n",
                            f"Alignment Verdicts:\n{prettify_list(self.alignment_verdicts)}\n",
                            f"Score: {self.score}\nReason: {self.reason}",
                        ],
                    )
                return self.score

    async def a_measure(
        self,
        test_case: Union[LLMTestCase, ConversationalTestCase],
        _show_indicator: bool = True,
    ) -> float:
        if isinstance(test_case, ConversationalTestCase):
            test_case = validate_conversational_test_case(test_case, self)
        check_llm_test_case_params(test_case, required_params, self)

        self.evaluation_cost = 0 if self.using_native_model else None
        with metric_progress_indicator(
            self,
            async_mode=True,
            _show_indicator=_show_indicator,
        ):
            self.truths, self.claims = await asyncio.gather(
                self._a_generate_claims(test_case.input),
                self._a_generate_claims(test_case.actual_output),
            )
            (
                self.coverage_verdicts,
                self.alignment_verdicts,
            ) = await asyncio.gather(
                self._a_generate_coverage_verdicts(test_case),
                self._a_generate_alignment_verdicts(),
            )

            self.alignment_score = self._calculate_score(ScoreType.ALIGNMENT)
            self.coverage_score = self._calculate_score(ScoreType.COVERAGE)
            score_breakdown = {
                ScoreType.ALIGNMENT.value: self.alignment_score,
                ScoreType.COVERAGE.value: self.coverage_score,
            }
            self.score = min(self.alignment_score, self.coverage_score)
            self.reason = await self._a_generate_reason()
            self.success = self.score >= self.threshold
            if self.verbose_mode:
                print_intermediate_steps(
                    self.__name__,
                    steps=[
                        f"Truths:\n{prettify_list(self.truths)}\n",
                        f"Claims:\n{prettify_list(self.claims)}\n",
                        f"Assessment Questions:\n{prettify_list(self.assessment_questions)}\n",
                        f"Coverage Verdicts:\n{prettify_list(self.coverage_verdicts)}\n",
                        f"Alignment Verdicts:\n{prettify_list(self.alignment_verdicts)}\n",
                        f"Score: {self.score}\nReason: {self.reason}",
                    ],
                )
            return self.score

    async def _measure_async(
        self,
        test_case: Union[LLMTestCase, ConversationalTestCase],
    ):
        await self.a_measure(test_case, _show_indicator=False)
        return (
            self.truths,
            self.claims,
            self.coverage_verdicts,
            self.alignment_verdicts,
            self.coverage_score,
            self.alignment_score,
            self.score_breakdown,
            self.score,
            self.reason,
            self.success,
        )

    async def _a_generate_reason(self) -> str:
        if self.include_reason is False:
            return None

        contradictions = []
        redundancies = []
        for verdict in self.alignment_verdicts:
            if verdict.verdict.strip().lower() == "no":
                contradictions.append(verdict.reason)
            elif verdict.verdict.strip().lower() == "idk":
                redundancies.append(verdict.reason)

        questions = []
        if self.coverage_verdicts:
            for verdict in self.coverage_verdicts:
                if (
                    verdict.original_verdict.strip().lower() == "yes"
                    and verdict.summary_verdict.strip().lower() == "no"
                ):
                    questions.append(verdict.question)

        prompt: dict = SummarizationTemplate.generate_reason(
            contradictions=contradictions,
            redundancies=redundancies,
            questions=questions,
            score=format(self.score, ".2f"),
        )

        if len(questions) > 0:
            prompt += f"""Questions the original text can answer but not the summary:
{questions}

"""
        prompt += """JSON:
"""

        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)

        data = trimAndLoadJson(res, self)
        return data["reason"]

    def _generate_reason(self) -> str:
        if self.include_reason is False:
            return None

        contradictions = []
        redundancies = []
        for verdict in self.alignment_verdicts:
            if verdict.verdict.strip().lower() == "no":
                contradictions.append(verdict.reason)
            elif verdict.verdict.strip().lower() == "idk":
                redundancies.append(verdict.reason)

        questions = []
        if self.coverage_verdicts:
            for verdict in self.coverage_verdicts:
                if (
                    verdict.original_verdict.strip().lower() == "yes"
                    and verdict.summary_verdict.strip().lower() == "no"
                ):
                    questions.append(verdict.question)

        prompt: dict = SummarizationTemplate.generate_reason(
            contradictions=contradictions,
            redundancies=redundancies,
            questions=questions,
            score=format(self.score, ".2f"),
        )

        if len(questions) > 0:
            prompt += f"""Questions the original text can answer but not the summary:
{questions}

"""
        prompt += """JSON:
"""

        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)

        data = trimAndLoadJson(res, self)
        return data["reason"]

    def _calculate_score(self, score_type: ScoreType) -> float:
        if score_type == ScoreType.ALIGNMENT:
            total = len(self.alignment_verdicts)
            if total == 0:
                return 0
            faithfulness_count = 0
            for verdict in self.alignment_verdicts:
                # Different from the faithfulness score, this
                # penalizes 'idk' (full of fluff) summaries
                if verdict.verdict.strip().lower() == "yes":
                    faithfulness_count += 1

            score = faithfulness_count / total

        else:
            if self.assessment_questions is None:
                return 1
            total = 0
            coverage_count = 0
            for verdict in self.coverage_verdicts:
                if verdict.original_verdict.strip().lower() == "yes":
                    total += 1
                    if verdict.summary_verdict.strip().lower() == "yes":
                        coverage_count += 1

            if total == 0:
                return 0

            score = coverage_count / total

        return 0 if self.strict_mode and score < self.threshold else score

    async def _a_generate_answers(self, text: str) -> List[str]:
        prompt = SummarizationTemplate.generate_answers(
            questions=self.assessment_questions, text=text
        )
        if self.using_native_model:
            res, cost = await self.model.a_generate(prompt)
            self.evaluation_cost += cost
        else:
            res = await self.model.a_generate(prompt)
        data = trimAndLoadJson(res, self)
        return data["answers"]

    def _generate_answers(self, text: str) -> List[str]:
        prompt = SummarizationTemplate.generate_answers(
            questions=self.assessment_questions, text=text
        )
        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)
        data = trimAndLoadJson(res, self)
        return data["answers"]

    async def _a_generate_assessment_questions(self, text: str):
        prompt = SummarizationTemplate.generate_questions(text=text, n=self.n)
        if self.using_native_model:
            res, cost = await self.model.a_generate(prompt)
            self.evaluation_cost += cost
        else:
            res = await self.model.a_generate(prompt)
        data = trimAndLoadJson(res, self)
        return data["questions"]

    def _generate_assessment_questions(self, text: str):
        prompt = SummarizationTemplate.generate_questions(text=text, n=self.n)
        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)
        data = trimAndLoadJson(res, self)
        return data["questions"]

    async def _a_generate_coverage_verdicts(
        self, test_case: LLMTestCase
    ) -> List[SummarizationCoverageVerdict]:
        if self.assessment_questions is None:
            self.assessment_questions = (
                await self._a_generate_assessment_questions(test_case.input)
            )

        tasks = [
            self._a_generate_answers(test_case.input),
            self._a_generate_answers(test_case.actual_output),
        ]
        results = await asyncio.gather(*tasks)
        original_answers = results[0]
        summary_answers = results[1]

        if len(original_answers) != len(summary_answers):
            raise ValueError("Number of verdicts generated does not equal.")

        coverage_veridcts: List[SummarizationCoverageVerdict] = []
        for i in range(len(original_answers)):
            coverage_veridcts.append(
                SummarizationCoverageVerdict(
                    summary_verdict=summary_answers[i],
                    original_verdict=original_answers[i],
                    question=self.assessment_questions[i],
                )
            )
        return coverage_veridcts

    def _generate_coverage_verdicts(
        self, test_case: LLMTestCase
    ) -> List[SummarizationCoverageVerdict]:
        if self.assessment_questions is None:
            self.assessment_questions = self._generate_assessment_questions(
                test_case.input
            )

        original_answers = self._generate_answers(test_case.input)
        summary_answers = self._generate_answers(test_case.actual_output)

        if len(original_answers) != len(summary_answers):
            raise ValueError("Number of verdicts generated does not equal.")

        coverage_veridcts: List[SummarizationCoverageVerdict] = []
        for i in range(len(original_answers)):
            coverage_veridcts.append(
                SummarizationCoverageVerdict(
                    summary_verdict=summary_answers[i],
                    original_verdict=original_answers[i],
                    question=self.assessment_questions[i],
                )
            )

        return coverage_veridcts

    async def _a_generate_alignment_verdicts(
        self,
    ) -> List[SummarizationAlignmentVerdict]:
        if len(self.claims) == 0:
            return []

        verdicts: List[SummarizationAlignmentVerdict] = []
        prompt = SummarizationTemplate.generate_alignment_verdicts(
            summary_claims=self.claims, orignal_text="\n\n".join(self.truths)
        )
        if self.using_native_model:
            res, cost = await self.model.a_generate(prompt)
            self.evaluation_cost += cost
        else:
            res = await self.model.a_generate(prompt)
        data = trimAndLoadJson(res, self)
        verdicts = [
            SummarizationAlignmentVerdict(**item) for item in data["verdicts"]
        ]
        return verdicts

    def _generate_alignment_verdicts(
        self,
    ) -> List[SummarizationAlignmentVerdict]:
        if len(self.claims) == 0:
            return []

        verdicts: List[SummarizationAlignmentVerdict] = []
        prompt = SummarizationTemplate.generate_alignment_verdicts(
            summary_claims=self.claims, orignal_text="\n\n".join(self.truths)
        )
        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)
        data = trimAndLoadJson(res, self)
        verdicts = [
            SummarizationAlignmentVerdict(**item) for item in data["verdicts"]
        ]
        return verdicts

    async def _a_generate_claims(self, text: str) -> List[str]:
        # Borrow faithfulness template
        prompt = FaithfulnessTemplate.generate_claims(text=text)
        if self.using_native_model:
            res, cost = await self.model.a_generate(prompt)
            self.evaluation_cost += cost
        else:
            res = await self.model.a_generate(prompt)
        data = trimAndLoadJson(res, self)
        return data["claims"]

    def _generate_claims(self, text: str) -> List[str]:
        # Borrow faithfulness template
        prompt = FaithfulnessTemplate.generate_claims(text=text)
        if self.using_native_model:
            res, cost = self.model.generate(prompt)
            self.evaluation_cost += cost
        else:
            res = self.model.generate(prompt)
        data = trimAndLoadJson(res, self)
        return data["claims"]

    def is_successful(self) -> bool:
        if self.error is not None:
            self.success = False
        else:
            try:
                self.success = self.score >= self.threshold
            except:
                self.success = False
        return self.success

    @property
    def __name__(self):
        return "Summarization"
