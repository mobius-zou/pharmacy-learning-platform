"""v1 路由汇总。"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    auth,
    cards,
    exam_points,
    experiments,
    literature,
    review,
    wrong_questions,
)

api_router = APIRouter()
api_router.include_router(auth.router)
api_router.include_router(cards.router)
api_router.include_router(review.router)
api_router.include_router(wrong_questions.router)
api_router.include_router(literature.router)
api_router.include_router(experiments.router)
api_router.include_router(exam_points.router)

__all__ = ["api_router"]
