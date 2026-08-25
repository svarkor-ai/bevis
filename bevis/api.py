"""Optional HTTP API — the same rules, over the wire.

This module is the ONLY part of bevis with third-party dependencies, and it is
an extra on purpose (`pip install bevis[api]`). The CLI must keep working on a
machine with nothing but Python, because that is where verification actually
has to happen: a build box, a container, someone's laptop at 2am.

Every endpoint calls the same functions in core.py that the CLI calls. There is
no second implementation of the close rule here to drift out of sync with the
first one — the refusals below are core.py's refusals, translated to HTTP
status codes.
"""
from __future__ import annotations

import hmac
import os
from typing import Optional

from . import __version__, core
from .db import connect, get_job
from .errors import NotFound, Refusal, UsageError

try:
    from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
except ImportError as exc:  # pragma: no cover - exercised by hand, not in CI
    raise SystemExit(
        "bevis serve needs the optional API extra: pip install 'bevis[api]'"
    ) from exc


def _authorise(authorization: Optional[str]) -> None:
    """Bearer token, only if $BEVIS_TOKEN is set.

    No token configured means no auth — appropriate for 127.0.0.1, and stated
    plainly rather than pretended otherwise. If you expose this beyond
    localhost, set the token.
    """
    expected = os.environ.get("BEVIS_TOKEN", "")
    if not expected:
        return
    supplied = (authorization or "")
    prefix = "Bearer "
    if not supplied.startswith(prefix) or not hmac.compare_digest(
            supplied[len(prefix):], expected):
        raise HTTPException(401, "missing or invalid bearer token")


def create_app(db_path):
    app = FastAPI(title="bevis", version=__version__,
                  description="A job cannot close without machine-checkable evidence.")

    def db():
        conn = connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    def auth(authorization: Optional[str] = Header(default=None)):
        _authorise(authorization)

    @app.exception_handler(Refusal)
    async def _refusal(request, exc):  # noqa: ANN001 - FastAPI handler signature
        from fastapi.responses import JSONResponse

        # 409: the request was well-formed, and a rule said no.
        return JSONResponse(status_code=409, content={"refused": str(exc)})

    @app.exception_handler(UsageError)
    async def _usage(request, exc):  # noqa: ANN001
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"error": str(exc)})

    @app.exception_handler(NotFound)
    async def _notfound(request, exc):  # noqa: ANN001
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"error": str(exc)})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/jobs", dependencies=[Depends(auth)])
    def list_jobs(status: Optional[str] = Query(default=None),
                  parent: Optional[str] = Query(default=None), conn=Depends(db)):
        return core.list_jobs(conn, status=status, parent=parent)

    @app.post("/jobs", status_code=201, dependencies=[Depends(auth)])
    def create_job(body: dict = Body(...), conn=Depends(db)):
        return core.create_job(
            conn, body.get("title"), body.get("acceptance"),
            description=body.get("description", ""), parent=body.get("parent_id"),
            after=body.get("after", ()), assignee=body.get("assignee", ""),
            actor=body.get("actor", ""))

    @app.get("/jobs/{ref}", dependencies=[Depends(auth)])
    def show_job(ref: str, conn=Depends(db)):
        job = core.job_dict(conn, get_job(conn, ref))
        job["checks"] = core.list_checks(conn, job["id"])
        job["runs"] = core.job_runs(conn, job["id"])
        return job

    @app.patch("/jobs/{ref}", dependencies=[Depends(auth)])
    def update_job(ref: str, body: dict = Body(...), conn=Depends(db)):
        # parent_id reaches core.update_job untouched so it produces the same
        # refusal the CLI produces, rather than being filtered out here.
        fields = {k: v for k, v in body.items() if k != "actor"}
        return core.update_job(conn, ref, actor=body.get("actor", ""), **fields)

    @app.get("/ready", dependencies=[Depends(auth)])
    def ready(conn=Depends(db)):
        return core.ready_jobs(conn)

    @app.post("/jobs/{ref}/claim", dependencies=[Depends(auth)])
    def claim(ref: str, body: dict = Body(default={}), conn=Depends(db)):
        return core.claim(conn, ref, actor=body.get("actor", ""))

    @app.post("/jobs/{ref}/status", dependencies=[Depends(auth)])
    def set_status(ref: str, body: dict = Body(...), conn=Depends(db)):
        return core.set_status(conn, ref, body.get("status"),
                               reason=body.get("reason", ""),
                               actor=body.get("actor", ""))

    @app.post("/jobs/{ref}/close", dependencies=[Depends(auth)])
    def close(ref: str, body: dict = Body(...), conn=Depends(db)):
        if body.get("run"):
            return core.close_by_running(conn, ref, body["run"],
                                         timeout=int(body.get("timeout",
                                                              core.DEFAULT_TIMEOUT)),
                                         actor=body.get("actor", ""))
        return core.close_job(conn, ref, body.get("verify_cmd"),
                              body.get("verify_exit"), body.get("verify_output"),
                              actor=body.get("actor", ""))

    @app.post("/jobs/{ref}/verify", dependencies=[Depends(auth)])
    def verify(ref: str, body: dict = Body(...), conn=Depends(db)):
        return core.verify_job(conn, ref, body.get("actor"), note=body.get("note", ""))

    @app.post("/jobs/{ref}/reopen", dependencies=[Depends(auth)])
    def reopen(ref: str, body: dict = Body(...), conn=Depends(db)):
        return core.reopen_job(conn, ref, body.get("reason"),
                               actor=body.get("actor", ""))

    @app.get("/jobs/{ref}/checks", dependencies=[Depends(auth)])
    def list_checks(ref: str, conn=Depends(db)):
        return core.list_checks(conn, ref)

    @app.post("/jobs/{ref}/checks", status_code=201, dependencies=[Depends(auth)])
    def add_check(ref: str, body: dict = Body(...), conn=Depends(db)):
        return core.add_check(conn, ref, body.get("name"), body.get("cmd"),
                              blocking=bool(body.get("blocking")),
                              actor=body.get("actor", ""))

    @app.post("/jobs/{ref}/checks/run", dependencies=[Depends(auth)])
    def run_checks(ref: str, body: dict = Body(default={}), conn=Depends(db)):
        return core.run_checks(conn, ref, name=body.get("name"),
                               actor=body.get("actor", ""))

    @app.get("/jobs/{ref}/events", dependencies=[Depends(auth)])
    def events(ref: str, conn=Depends(db)):
        return core.job_events(conn, ref)

    @app.post("/reclaim", dependencies=[Depends(auth)])
    def reclaim(body: dict = Body(default={}), conn=Depends(db)):
        return core.reclaim(conn, stale=body.get("stale", "30m"),
                            actor=body.get("actor", ""))

    return app


def serve(db_path, host="127.0.0.1", port=8420):  # pragma: no cover - process entry
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "bevis serve needs the optional API extra: pip install 'bevis[api]'"
        ) from exc
    if not os.environ.get("BEVIS_TOKEN") and host not in ("127.0.0.1", "localhost", "::1"):
        print("bevis: warning — serving on %s with no BEVIS_TOKEN set; "
              "anyone who can reach this port can write to the board." % host)
    uvicorn.run(create_app(db_path), host=host, port=port)
