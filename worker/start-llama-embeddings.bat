@echo off
rem Start a second llama-server on this machine, the one that answers with
rem vectors instead of sentences: another model, another port, and the three
rem flags the OpenAI embeddings route needs. Everything it does is in
rem enggraph_worker\runserver.py. Extra arguments go straight to llama-server:
rem   start-llama-embeddings.bat --port 8090
rem   start-llama-embeddings.bat --host 0.0.0.0
rem
rem The chat server started by start-llama-server.bat can keep running: one
rem llama-server serves one model, and summarizing and embedding are two.
setlocal
cd /d "%~dp0"
set PY=py
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
"%PY%" -m enggraph_worker.runserver --install --embeddings %*
pause
