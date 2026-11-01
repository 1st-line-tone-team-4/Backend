"""Language Learning Platform A-part API."""


def main() -> None:
    import uvicorn

    uvicorn.run("language_learning_api.main:app", host="127.0.0.1", port=8000, reload=True)
