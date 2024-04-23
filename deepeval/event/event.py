from typing import Optional, List, Dict
from deepeval.api import Api, Endpoints
from deepeval.event.api import APIEvent, EventHttpResponse


def track(
    event_name: str,
    model: str,
    input: str,
    response: str,
    retrieval_context: Optional[List[str]] = None,
    completion_time: Optional[float] = None,
    token_usage: Optional[float] = None,
    token_cost: Optional[float] = None,
    distinct_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
    additional_data: Optional[Dict] = None,
    fail_silently: Optional[bool] = False,
    raise_expection: Optional[bool] = False,
    run_async: Optional[bool] = True,
) -> str:
    try:
        api_event = APIEvent(
            name=event_name,
            model=model,
            input=input,
            response=response,
            retrievalContext=retrieval_context,
            completionTime=completion_time,
            tokenUsage=token_usage,
            tokenCost=token_cost,
            distinctId=distinct_id,
            conversationId=conversation_id,
            additionalData=additional_data,
        )
        api = Api()
        try:
            body = api_event.model_dump(by_alias=True, exclude_none=True)
        except AttributeError:
            # Pydantic version below 2.0
            body = api_event.dict(by_alias=True, exclude_none=True)

        result = api.post_request(
            endpoint=Endpoints.EVENT_ENDPOINT.value,
            body=body,
        )
        response = EventHttpResponse(eventId=result["eventId"])
        return response.eventId
    except Exception as e:
        if fail_silently:
            return

        if raise_expection:
            raise (e)
        else:
            print(str(e))