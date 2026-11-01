"""Unified A+B server: uvicorn language_learning_api.main:create_app --factory."""

from app.main import create_app

__all__ = ["create_app"]
