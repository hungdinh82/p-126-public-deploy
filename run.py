import uvicorn

from server.config import settings


if __name__ == "__main__":
    uvicorn.run("server.app:app", host=settings.host, port=settings.port, reload=False)

