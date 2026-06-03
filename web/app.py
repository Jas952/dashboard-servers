#!/usr/bin/env python3
"""
Pearl Web Interface - FastAPI backend for CLI tools
"""

import os
import sys
import json
import time
import subprocess
import threading
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import uvicorn

# Paths
BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent
CORE_DIR = ROOT_DIR / "core"
DATA_DIR = ROOT_DIR / "data"
LOGS_DIR = ROOT_DIR / "logs"

app = FastAPI(title="Pearl Web Interface", version="1.0.0")

# Connected WebSocket clients
connected_clients: List[WebSocket] = []

# Process management
running_processes: Dict[str, subprocess.Popen] = {}
process_logs: Dict[str, List[str]] = {}
process_status: Dict[str, dict] = {}

# Data cache
_data_cache: Dict[str, Any] = {}
_data_lock = threading.Lock()


# ==================== Models ====================

class CommandRequest(BaseModel):
    command: str
    args: List[str] = []


class FilterUpdate(BaseModel):
    name: str
    value: Any


# ==================== WebSocket Manager ====================

async def broadcast_message(message: dict):
    """Broadcast message to all connected clients"""
    disconnected = []
    for client in connected_clients:
        try:
            await client.send_json(message)
        except:
            disconnected.append(client)
    
    for client in disconnected:
        if client in connected_clients:
            connected_clients.remove(client)


# ==================== Process Management ====================

def read_process_output(process_id: str, process: subprocess.Popen, script_name: str):
    """Read process output and broadcast to clients"""
    try:
        while True:
            line = process.stdout.readline()
            if not line:
                break
            
            line_str = line.decode('utf-8', errors='replace').rstrip()
            timestamp = datetime.now().strftime("%H:%M:%S")
            
            if process_id not in process_logs:
                process_logs[process_id] = []
            
            process_logs[process_id].append(f"[{timestamp}] {line_str}")
            # Keep last 500 lines
            if len(process_logs[process_id]) > 500:
                process_logs[process_id] = process_logs[process_id][-500:]
            
            # Broadcast to clients
            asyncio.run_coroutine_threadsafe(
                broadcast_message({
                    "type": "log",
                    "process": script_name,
                    "process_id": process_id,
                    "timestamp": timestamp,
                    "message": line_str
                }),
                asyncio.get_event_loop()
            )
        
        # Process ended
        process.wait()
        exit_code = process.returncode
        
        process_status[process_id] = {
            "running": False,
            "exit_code": exit_code,
            "ended_at": datetime.now().isoformat()
        }
        
        if process_id in running_processes:
            del running_processes[process_id]
        
        asyncio.run_coroutine_threadsafe(
            broadcast_message({
                "type": "process_ended",
                "process": script_name,
                "process_id": process_id,
                "exit_code": exit_code
            }),
            asyncio.get_event_loop()
        )
        
    except Exception as e:
        print(f"Error reading process output: {e}")


# ==================== API Endpoints ====================

@app.get("/", response_class=HTMLResponse)
async def root():
    """Serve main HTML page"""
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/status")
async def get_status():
    """Get system status"""
    return {
        "running_processes": [
            {
                "id": pid,
                "script": info["script"],
                "started_at": info.get("started_at"),
                "status": "running"
            }
            for pid, info in process_status.items()
            if info.get("running", False)
        ],
        "data_dir_exists": DATA_DIR.exists(),
        "core_dir_exists": CORE_DIR.exists()
    }


@app.post("/api/run/{script_name}")
async def run_script(script_name: str, background_tasks: BackgroundTasks):
    """Start a CLI script"""
    script_map = {
        "watchdog": "watchdog.py",
        "fleet-live": "fleet-live.py",
        "autobuy": "autobuy.py",
        "dashboard": "dashboard.py"
    }
    
    if script_name not in script_map:
        raise HTTPException(status_code=404, detail=f"Unknown script: {script_name}")
    
    script_file = script_map[script_name]
    script_path = CORE_DIR / script_file
    
    if not script_path.exists():
        raise HTTPException(status_code=404, detail=f"Script not found: {script_file}")
    
    # Check if already running
    for pid, info in process_status.items():
        if info.get("script") == script_name and info.get("running", False):
            return {"status": "already_running", "process_id": pid}
    
    process_id = f"{script_name}_{int(time.time())}"
    
    try:
        process = subprocess.Popen(
            [sys.executable, str(script_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT_DIR),
            bufsize=1,
            universal_newlines=False
        )
        
        running_processes[process_id] = process
        process_logs[process_id] = []
        process_status[process_id] = {
            "script": script_name,
            "running": True,
            "started_at": datetime.now().isoformat(),
            "pid": process.pid
        }
        
        # Start output reader thread
        thread = threading.Thread(
            target=read_process_output,
            args=(process_id, process, script_name),
            daemon=True
        )
        thread.start()
        
        return {
            "status": "started",
            "process_id": process_id,
            "pid": process.pid
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/stop/{process_id}")
async def stop_process(process_id: str):
    """Stop a running process"""
    if process_id not in running_processes:
        raise HTTPException(status_code=404, detail="Process not found")
    
    process = running_processes[process_id]
    
    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        
        process_status[process_id]["running"] = False
        process_status[process_id]["exit_code"] = process.returncode
        
        del running_processes[process_id]
        
        return {"status": "stopped", "exit_code": process.returncode}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/logs/{process_id}")
async def get_logs(process_id: str, lines: int = 100):
    """Get logs for a process"""
    if process_id not in process_logs:
        return {"logs": [], "running": False}
    
    logs = process_logs.get(process_id, [])
    return {
        "logs": logs[-lines:] if logs else [],
        "running": process_status.get(process_id, {}).get("running", False)
    }


@app.get("/api/data/{data_type}")
async def get_data(data_type: str):
    """Get data from JSON files"""
    file_map = {
        "instances": None,  # Fetched via vastai command
        "blacklist": "blacklist.json",
        "history": "history.json",
        "autobuy_state": "autobuy_state.json",
        "global_logs": "global_logs.jsonl"
    }
    
    if data_type == "instances":
        # Fetch from vastai
        try:
            result = subprocess.run(
                ["vastai", "show", "instances", "--raw"],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                # Ensure we return a list
                if isinstance(data, dict):
                    # Sometimes vastai returns {instances: [...]}
                    return data.get('instances', []) if 'instances' in data else [data]
                return data if isinstance(data, list) else []
            else:
                # Return error info for debugging
                return {
                    "error": "vastai command failed",
                    "stderr": result.stderr,
                    "returncode": result.returncode
                }
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=504, detail="vastai command timed out")
        except FileNotFoundError:
            raise HTTPException(status_code=503, detail="vastai CLI not found. Please install: pip install vastai")
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=500, detail=f"Invalid JSON from vastai: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
    
    if data_type not in file_map:
        raise HTTPException(status_code=404, detail=f"Unknown data type: {data_type}")
    
    filename = file_map[data_type]
    if not filename:
        raise HTTPException(status_code=404, detail="Data type not file-based")
    
    filepath = DATA_DIR / filename
    if not filepath.exists():
        return {}
    
    try:
        with open(filepath, 'r') as f:
            if filename.endswith('.jsonl'):
                lines = f.readlines()
                return [json.loads(line) for line in lines if line.strip()][-100:]
            return json.load(f)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config")
async def get_config():
    """Get current configuration from core scripts"""
    config = {
        "filters": {},
        "gpu_prices": {},
        "expected_hashrate": {}
    }
    
    # Read autobuy.py for filters
    autobuy_path = CORE_DIR / "autobuy.py"
    if autobuy_path.exists():
        try:
            content = autobuy_path.read_text()
            # Simple extraction of _filters dict
            import re
            # Extract EXPECTED hashrates
            expected_match = re.search(r'EXPECTED\s*=\s*\{([^}]+)\}', content)
            if expected_match:
                expected_str = "{" + expected_match.group(1) + "}"
                try:
                    config["expected_hashrate"] = eval(expected_str)
                except:
                    pass
        except:
            pass
    
    return config


@app.post("/api/instances/{instance_id}/destroy")
async def destroy_instance(instance_id: str):
    """Destroy a Vast.ai instance"""
    try:
        # First try vastai destroy
        result = subprocess.run(
            ["vastai", "destroy", "instance", instance_id],
            input="y\n",
            capture_output=True,
            text=True,
            timeout=30
        )
        
        success = result.returncode == 0 and "destroying" in result.stdout.lower()
        
        # Check if it was a DO instance (starts with 574 or is long)
        if not success and (instance_id.startswith("574") or len(instance_id) >= 9):
            # Try DO destroy via curl
            do_key = "YOUR_DO_API_TOKEN"
            import urllib.request
            req = urllib.request.Request(
                f"https://api.digitalocean.com/v2/droplets/{instance_id}",
                method="DELETE"
            )
            req.add_header("Authorization", f"Bearer {do_key}")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    success = resp.status in (204, 200, 202)
            except:
                pass
        
        if success:
            return {"status": "destroyed", "instance_id": instance_id}
        else:
            raise HTTPException(status_code=500, detail="Failed to destroy instance")
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/blacklist/{machine_id}")
async def remove_from_blacklist(machine_id: str):
    """Remove a machine from the blacklist"""
    try:
        blacklist_path = DATA_DIR / "blacklist.json"
        if not blacklist_path.exists():
            raise HTTPException(status_code=404, detail="Blacklist not found")
        
        with open(blacklist_path, 'r') as f:
            blacklist = json.load(f)
        
        if machine_id not in blacklist:
            raise HTTPException(status_code=404, detail="Machine not in blacklist")
        
        del blacklist[machine_id]
        
        with open(blacklist_path, 'w') as f:
            json.dump(blacklist, f, indent=2)
        
        return {"status": "removed", "machine_id": machine_id}
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ==================== WebSocket Endpoint ====================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    
    try:
        # Send initial status
        await websocket.send_json({
            "type": "connected",
            "message": "Connected to Pearl Web Interface"
        })
        
        # Send current process status
        for pid, info in process_status.items():
            await websocket.send_json({
                "type": "process_status",
                "process_id": pid,
                "status": info
            })
        
        # Keep connection alive and handle client messages
        while True:
            try:
                data = await websocket.receive_json()
                # Handle client commands if needed
                if data.get("action") == "ping":
                    await websocket.send_json({"type": "pong"})
                    
            except:
                break
                
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


# ==================== Static Files ====================

# Serve static files
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


if __name__ == "__main__":
    print("=" * 60)
    print("  Pearl Web Interface")
    print("=" * 60)
    print("  Open: http://localhost:8000")
    print("=" * 60)
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
