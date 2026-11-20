from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response

from aris.core.contracts import DomainError
from aris.interfaces.commands.api import authorization, bounded_input
from aris.interfaces.policy.drafts import PolicyDraftInput
from aris.interfaces.policy.history import PolicyHistoryAssessmentInput, PolicyHistoryWindowsInput
from aris.interfaces.policy.material_report import REPORT_CSP, comparison_report
from aris.interfaces.policy.materials import (
    PolicyMaterialComparisonInput,
    PolicyMaterialDetailInput,
    PolicyMaterialTextInput,
)
from aris.interfaces.policy.service import PolicyDetailInput, PolicyReadSurface
from aris.modules.nova.policy_catalog_schemas import (
    PolicySourceComparisonQuery,
    PolicySourceListQuery,
)
from aris.modules.nova.policy_feedback_schemas import PolicyFeedbackListQuery, RecordPolicyFeedback
from aris.modules.nova.policy_material_schemas import (
    PolicyMaterialListQuery,
    RecordPolicyReviewMaterial,
)

ASSETS = Path(__file__).with_name("static")
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
}


def build_router(service: PolicyReadSurface) -> APIRouter:
    router = APIRouter(prefix="/internal/policy", tags=["policy-source-review"])

    if service.feedback is not None:

        @router.post("/feedback")
        async def feedback(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected feedback query parameters", 422)
            query = await bounded_input(request, PolicyFeedbackListQuery)
            result = await run_in_threadpool(
                service.feedback_list, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

        if service.draft_commands is not None and service.draft_commands.feedback_writing:

            @router.post("/feedback/validate")
            async def validate_feedback(request: Request) -> JSONResponse:
                if request.query_params:
                    raise DomainError(
                        "VALIDATION_ERROR", "Unexpected feedback query parameters", 422
                    )
                query = await bounded_input(request, RecordPolicyFeedback)
                result = await run_in_threadpool(
                    service.feedback_validate,
                    authorization(request),
                    request.state.request_id,
                    query,
                )
                return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    if service.material_comparison_implementation_ref is not None:

        @router.post("/materials/compare")
        async def materials_compare(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected material query parameters", 422)
            query = await bounded_input(request, PolicyMaterialComparisonInput)
            result = await run_in_threadpool(
                service.materials_compare, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

        @router.post("/materials/report")
        async def materials_report(request: Request) -> HTMLResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected material query parameters", 422)
            query = await bounded_input(request, PolicyMaterialComparisonInput)
            result = await run_in_threadpool(
                service.materials_compare, authorization(request), request.state.request_id, query
            )
            return HTMLResponse(
                comparison_report(result),
                headers={
                    **PAGE_HEADERS,
                    "Content-Security-Policy": REPORT_CSP + "; frame-ancestors 'none'",
                    "Content-Disposition": (
                        'attachment; filename="aris-policy-case-comparison.html"'
                    ),
                },
            )

    if (
        service.draft_commands is not None
        and service.draft_commands.material_implementation_ref is not None
    ):

        @router.post("/materials/validate")
        async def materials_validate(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected material query parameters", 422)
            query = await bounded_input(request, RecordPolicyReviewMaterial)
            result = await run_in_threadpool(
                service.materials_validate, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

        @router.post("/materials/validate-text")
        async def materials_validate_text(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected material query parameters", 422)
            query = await bounded_input(request, PolicyMaterialTextInput)
            result = await run_in_threadpool(
                service.materials_validate_text,
                authorization(request),
                request.state.request_id,
                query,
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    if service.review_materials is not None:

        @router.post("/materials/query")
        async def materials_query(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected material query parameters", 422)
            query = await bounded_input(request, PolicyMaterialListQuery)
            result = await run_in_threadpool(
                service.materials_list, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

        @router.post("/materials/read")
        async def materials_read(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected material query parameters", 422)
            query = await bounded_input(request, PolicyMaterialDetailInput)
            result = await run_in_threadpool(
                service.materials_detail, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    if service.historical_usage is not None:

        @router.post("/history/windows")
        async def history_windows(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected historical query parameters", 422)
            query = await bounded_input(request, PolicyHistoryWindowsInput)
            result = await run_in_threadpool(
                service.history_windows, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

        @router.post("/history/assess")
        async def history_assess(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected historical query parameters", 422)
            query = await bounded_input(request, PolicyHistoryAssessmentInput)
            result = await run_in_threadpool(
                service.history_assess, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    if service.draft_commands is not None:

        @router.post("/drafts/validate")
        async def validate_draft(request: Request) -> JSONResponse:
            if request.query_params:
                raise DomainError("VALIDATION_ERROR", "Unexpected draft query parameters", 422)
            query = await bounded_input(request, PolicyDraftInput)
            result = await run_in_threadpool(
                service.validate_draft, authorization(request), request.state.request_id, query
            )
            return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/session")
    async def session(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        async for chunk in request.stream():
            if chunk:
                raise DomainError("VALIDATION_ERROR", "Session request requires an empty body", 422)
        result = await run_in_threadpool(
            service.session, authorization(request), request.state.request_id
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/sources")
    async def sources(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected policy query parameters", 422)
        query = await bounded_input(request, PolicySourceListQuery)
        result = await run_in_threadpool(
            service.sources, authorization(request), request.state.request_id, query
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/detail")
    async def detail(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected policy query parameters", 422)
        query = await bounded_input(request, PolicyDetailInput)
        result = await run_in_threadpool(
            service.detail, authorization(request), request.state.request_id, query
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.post("/compare")
    async def compare(request: Request) -> JSONResponse:
        if request.query_params:
            raise DomainError("VALIDATION_ERROR", "Unexpected policy query parameters", 422)
        query = await bounded_input(request, PolicySourceComparisonQuery)
        result = await run_in_threadpool(
            service.compare, authorization(request), request.state.request_id, query
        )
        return JSONResponse(result.model_dump(mode="json"), headers=PAGE_HEADERS)

    @router.get("/ui", response_class=HTMLResponse)
    def page() -> HTMLResponse:
        return HTMLResponse((ASSETS / "index.html").read_text(), headers=PAGE_HEADERS)

    @router.get("/ui/assets/{name}")
    def asset(
        name: Literal[
            "policy.js",
            "policy-scenarios.js",
            "policy-fields.js",
            "policy-history.js",
            "policy-materials.js",
            "policy-material-save.js",
            "policy-material-compare.js",
            "policy-feedback.js",
            "policy-feedback.css",
            "policy-editor.js",
            "policy.css",
        ],
    ) -> Response:
        return Response(
            (ASSETS / name).read_bytes(),
            headers=PAGE_HEADERS,
            media_type="text/javascript" if name.endswith(".js") else "text/css",
        )

    return router
