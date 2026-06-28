import sys
sys.path.insert(0, 'agents')
import uvicorn
uvicorn.run('server.fastapi_server:app', host='0.0.0.0', port=8090, log_level='warning')
