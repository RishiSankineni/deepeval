from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from contextlib import contextmanager
import sys
from typing import List, Optional, Union
import time
import asyncio

from deepeval.metrics import BaseMetric
from deepeval.test_case import LLMTestCase
from deepeval.utils import show_indicator
from deepeval.test_run import test_run_manager, APITestCase, MetricMetadata
from deepeval.test_run.cache import CachedTestCase, Cache


def format_metric_description(
    metric: BaseMetric, async_mode: Optional[bool] = None
):
    if async_mode is None:
        run_async = metric.async_mode
    else:
        run_async = async_mode

    if run_async:
        is_async = "yes"
    else:
        is_async = "no"

    return f"✨ You're running DeepEval's latest [rgb(106,0,255)]{metric.__name__} Metric[/rgb(106,0,255)]! [rgb(55,65,81)](using {metric.evaluation_model}, strict={metric.strict_mode}, async_mode={run_async})...[/rgb(55,65,81)]"


@contextmanager
def metric_progress_indicator(
    metric: BaseMetric,
    async_mode: Optional[bool] = None,
    _show_indicator: bool = True,
    total: int = 9999,
    transient: bool = True,
):
    console = Console(file=sys.stderr)  # Direct output to standard error
    if _show_indicator and show_indicator():
        with Progress(
            SpinnerColumn(style="rgb(106,0,255)"),
            TextColumn("[progress.description]{task.description}"),
            console=console,  # Use the custom console
            transient=transient,
        ) as progress:
            progress.add_task(
                description=format_metric_description(metric, async_mode),
                total=total,
            )
            yield
    else:
        yield


async def measure_metric_task(
    task_id,
    progress,
    metric: BaseMetric,
    test_case: LLMTestCase,
    cached_test_case: Union[CachedTestCase, None],
):
    while not progress.finished:
        start_time = time.perf_counter()
        metric_metadata = None
        if cached_test_case is not None:
            print(cached_test_case, "@@@@")
            cached_metric_data = Cache.get_metric_data(metric, cached_test_case)
            print(cached_metric_data, "$$$$$$$$$$$$")
            if cached_metric_data:
                metric_metadata = cached_metric_data.metric_metadata

        if metric_metadata:
            ## only change metric state, not configs
            metric.score = metric_metadata.score
            metric.success = metric_metadata.success
            metric.reason = metric_metadata.reason
            finish_text = "Read from Cache"
        else:
            try:
                await metric.a_measure(test_case, _show_indicator=False)
            except TypeError:
                await metric.a_measure(test_case)
            finally:
                finish_text = "Done"

        end_time = time.perf_counter()
        time_taken = format(end_time - start_time, ".2f")
        progress.update(task_id, advance=100)
        progress.update(
            task_id,
            description=f"{progress.tasks[task_id].description} [rgb(25,227,160)]{finish_text}! ({time_taken}s)",
        )
        break


async def measure_metrics_with_indicator(
    metrics: List[BaseMetric],
    test_case: LLMTestCase,
    cached_test_case: Union[CachedTestCase, None],
):
    if show_indicator():
        with Progress(
            SpinnerColumn(style="rgb(106,0,255)"),
            TextColumn("[progress.description]{task.description}"),
            transient=False,
        ) as progress:
            tasks = []
            for metric in metrics:
                task_id = progress.add_task(
                    description=format_metric_description(
                        metric, async_mode=True
                    ),
                    total=100,
                )
                tasks.append(
                    measure_metric_task(
                        task_id, progress, metric, test_case, cached_test_case
                    )
                )
            await asyncio.gather(*tasks)
    else:
        tasks = []
        for metric in metrics:
            metric_metadata = None
            if cached_test_case is not None:
                cached_metric_data = Cache.get_metric_data(
                    metric, cached_test_case
                )
                if cached_metric_data:
                    metric_metadata = cached_metric_data.metric_metadata

            if metric_metadata:
                ## Here we're setting the metric state from metrics metadata cache,
                ## and later using the metric state to create a new metrics metadata cache
                ## WARNING: Potential for bugs, what will happen if a metric changes state in between
                ## test cases?
                metric.score = metric_metadata.score
                metric.threshold = metric_metadata.threshold
                metric.success = metric_metadata.success
                metric.reason = metric_metadata.reason
                metric.strict_mode = metric_metadata.strict_mode
                metric.evaluation_model = metric_metadata.evaluation_model
            else:
                try:
                    tasks.append(
                        metric.a_measure(test_case, _show_indicator=False)
                    )
                except TypeError:
                    tasks.append(metric.a_measure(test_case))

        await asyncio.gather(*tasks)
