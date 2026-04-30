# www_server.py — Serves the www.elabsai.com marketing page on port 8002
# Run: python www_server.py
import os
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI()
_WWW_DIR = os.path.join(os.path.dirname(__file__), "..", "www")

@app.get("/", response_class=FileResponse)
def index():
    return FileResponse(os.path.join(_WWW_DIR, "index.html"))

app.mount("/", StaticFiles(directory=_WWW_DIR, html=True), name="www")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8002)
