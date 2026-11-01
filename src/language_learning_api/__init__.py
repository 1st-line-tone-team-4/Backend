"""A learning routes and compatibility entrypoint for the integrated server."""


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:create_app", factory=True, host="127.0.0.1", port=8000, access_log=False)
