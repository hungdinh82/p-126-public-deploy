import uvicorn

from src.vivi.config import settings


if __name__ == "__main__":
    uvicorn.run("src.vivi.api.app:app", host=settings.host, port=settings.port, reload=False)
