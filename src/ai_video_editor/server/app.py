"""FastAPI application factory for the VX sidecar."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import CFG, router


def create_app() -> FastAPI:
    app = FastAPI(
        title="VX Sidecar",
        version="0.1.0",
        summary="Local HTTP/WebSocket bridge between the VX macOS app and the Python pipeline.",
    )
    # The native app talks over loopback; allow any local origin for dev WebViews/tools.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    @app.get("/")
    def root():
        return {
            "service": "vx-sidecar",
            "library": str(CFG.library_dir.resolve()),
            "docs": "/docs",
        }

    return app


app = create_app()
