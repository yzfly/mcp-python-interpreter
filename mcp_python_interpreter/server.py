"""
MCP Python Interpreter

A Model Context Protocol server for interacting with Python environments 
and executing Python code. Supports both in-process execution (default, fast)
and subprocess execution (for environment isolation).
"""

import os
import sys
import json
import subprocess
import tempfile
import argparse
import traceback
import builtins
from pathlib import Path
from io import StringIO
from typing import Dict, List, Optional, Any
import asyncio
import concurrent.futures

# Import MCP SDK
from mcp.server.fastmcp import FastMCP

# Parse command line arguments
parser = argparse.ArgumentParser(description='MCP Python Interpreter')
parser.add_argument('--dir', type=str, default=os.getcwd(),
                    help='Working directory for code execution and file operations')
parser.add_argument('--python-path', type=str, default=None,
                    help='Custom Python interpreter path to use as default')
args, unknown = parser.parse_known_args()

# Configuration
ALLOW_SYSTEM_ACCESS = os.environ.get('MCP_ALLOW_SYSTEM_ACCESS', 'false').lower() in ('true', '1', 'yes')
WORKING_DIR = Path(args.dir).absolute()
WORKING_DIR.mkdir(parents=True, exist_ok=True)
DEFAULT_PYTHON_PATH = args.python_path if args.python_path else sys.executable

# Startup message
print(f"MCP Python Interpreter starting in directory: {WORKING_DIR}", file=sys.stderr)
print(f"Using default Python interpreter: {DEFAULT_PYTHON_PATH}", file=sys.stderr)
print(f"System-wide file access: {'ENABLED' if ALLOW_SYSTEM_ACCESS else 'DISABLED'}", file=sys.stderr)
print(f"Platform: {sys.platform}", file=sys.stderr)

# Create MCP server
mcp = FastMCP("python-interpreter")

# Thread pool for subprocess fallback
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

# ============================================================================
# REPL Session Management (for in-process execution)
# ============================================================================

class ReplSession:
    """Manages a Python REPL session with persistent state."""
    
    def __init__(self):
        self.locals = {
            "__builtins__": builtins,
            "__name__": "__main__",
            "__doc__": None,
            "__package__": None,
        }
        self.history = []
        
    def execute(self, code: str, timeout: Optional[int] = None) -> Dict[str, Any]:
        """
        Execute Python code in this session.
        
        Args:
            code: Python code to execute
            timeout: Optional timeout (not enforced for inline execution)
            
        Returns:
            Dict with stdout, stderr, result, and status
        """
        stdout_capture = StringIO()
        stderr_capture = StringIO()
        
        # Save original streams
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout_capture, stderr_capture
        
        result_value = None
        status = 0
        
        try:
            # Change to working directory for execution
            old_cwd = os.getcwd()
            os.chdir(WORKING_DIR)
            
            try:
                # Try to evaluate as expression first
                try:
                    result_value = eval(code, self.locals)
                    if result_value is not None:
                        print(repr(result_value))
                except SyntaxError:
                    # If not an expression, execute as statement
                    exec(code, self.locals)
                    
            except Exception:
                traceback.print_exc()
                status = 1
            finally:
                os.chdir(old_cwd)
                
        finally:
            # Restore original streams
            sys.stdout, sys.stderr = old_stdout, old_stderr
            
        return {
            "stdout": stdout_capture.getvalue(),
            "stderr": stderr_capture.getvalue(),
            "result": result_value,
            "status": status
        }

# Global sessions storage
_sessions: Dict[str, ReplSession] = {}

def get_session(session_id: str = "default") -> ReplSession:
    """Get or create a REPL session."""
    if session_id not in _sessions:
        _sessions[session_id] = ReplSession()
    return _sessions[session_id]

# ============================================================================
# Helper functions
# ============================================================================

def is_path_allowed(path: Path) -> bool:
    """Check if a path is allowed based on security settings."""
    if ALLOW_SYSTEM_ACCESS:
        return True
    
    try:
        path.resolve().relative_to(WORKING_DIR.resolve())
        return True
    except ValueError:
        return False


def _run_subprocess_sync(
    cmd: List[str],
    cwd: Optional[str] = None,
    timeout: int = 300
) -> Dict[str, Any]:
    """Synchronous subprocess execution for Windows compatibility."""
    try:
        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = subprocess.CREATE_NO_WINDOW
            try:
                creation_flags |= subprocess.CREATE_NEW_PROCESS_GROUP
            except AttributeError:
                pass
        
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creation_flags if sys.platform == "win32" else 0,
            encoding='utf-8',
            errors='replace',
            stdin=subprocess.DEVNULL
        )
        
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "status": result.returncode
        }
        
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode('utf-8', errors='replace') if e.stdout else ""
        stderr = e.stderr.decode('utf-8', errors='replace') if e.stderr else ""
        
        return {
            "stdout": stdout,
            "stderr": stderr + f"\nExecution timed out after {timeout} seconds",
            "status": -1
        }
        
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Error executing command: {str(e)}",
            "status": -1
        }


async def run_subprocess_async(
    cmd: List[str],
    cwd: Optional[str] = None,
    timeout: int = 300,
    input_data: Optional[str] = None
) -> Dict[str, Any]:
    """Run subprocess with Windows compatibility."""
    
    # On Windows, use thread pool for reliability
    if sys.platform == "win32":
        if input_data:
            print("Warning: input_data not supported on Windows sync mode", file=sys.stderr)
        
        loop = asyncio.get_event_loop()
        from functools import partial
        func = partial(_run_subprocess_sync, cmd, cwd, timeout)
        result = await loop.run_in_executor(_executor, func)
        return result
    
    # On Unix, use asyncio
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
            cwd=cwd
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=input_data.encode('utf-8') if input_data else None),
                timeout=timeout
            )
            
            return {
                "stdout": stdout.decode('utf-8', errors='replace'),
                "stderr": stderr.decode('utf-8', errors='replace'),
                "status": process.returncode
            }
            
        except asyncio.TimeoutError:
            try:
                process.kill()
                await process.wait()
            except:
                pass
            
            return {
                "stdout": "",
                "stderr": f"Execution timed out after {timeout} seconds",
                "status": -1
            }
            
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Error executing command: {str(e)}",
            "status": -1
        }


async def execute_python_code_subprocess(
    code: str, 
    python_path: Optional[str] = None,
    working_dir: Optional[str] = None,
    timeout: int = 300
) -> Dict[str, Any]:
    """Execute Python code via subprocess (for environment isolation)."""
    if python_path is None:
        python_path = DEFAULT_PYTHON_PATH
    
    temp_file = None
    try:
        fd, temp_file = tempfile.mkstemp(suffix='.py', text=True)
        
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(code)
                f.flush()
                os.fsync(f.fileno())
        except Exception as e:
            os.close(fd)
            raise e
        
        if sys.platform == "win32":
            await asyncio.sleep(0.05)
            temp_file = os.path.abspath(temp_file)
            if working_dir:
                working_dir = os.path.abspath(working_dir)
        
        result = await run_subprocess_async(
            [python_path, temp_file],
            cwd=working_dir,
            timeout=timeout
        )
        
        return result
        
    finally:
        if temp_file:
            try:
                if sys.platform == "win32":
                    await asyncio.sleep(0.05)
                
                if os.path.exists(temp_file):
                    os.unlink(temp_file)
            except Exception as e:
                print(f"Warning: Could not delete temp file {temp_file}: {e}", file=sys.stderr)


def get_python_environments() -> List[Dict[str, str]]:
    """Get all available Python environments."""
    environments = []
    
    if DEFAULT_PYTHON_PATH != sys.executable:
        try:
            result = subprocess.run(
                [DEFAULT_PYTHON_PATH, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
                capture_output=True, text=True, check=True, timeout=10,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            version = result.stdout.strip()
            
            environments.append({
                "name": "default",
                "path": DEFAULT_PYTHON_PATH,
                "version": version
            })
        except Exception as e:
            print(f"Error getting version for custom Python path: {e}", file=sys.stderr)
    
    environments.append({
        "name": "system",
        "path": sys.executable,
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    })
    
    # Try conda environments
    try:
        result = subprocess.run(
            ["conda", "info", "--envs", "--json"],
            capture_output=True, text=True, check=False, timeout=10,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        
        if result.returncode == 0:
            conda_info = json.loads(result.stdout)
            for env in conda_info.get("envs", []):
                env_name = os.path.basename(env)
                if env_name == "base":
                    env_name = "conda-base"
                
                python_path = os.path.join(env, "bin", "python")
                if not os.path.exists(python_path):
                    python_path = os.path.join(env, "python.exe")
                
                if os.path.exists(python_path):
                    try:
                        version_result = subprocess.run(
                            [python_path, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"],
                            capture_output=True, text=True, check=True, timeout=10,
                            stdin=subprocess.DEVNULL,
                            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
                        )
                        version = version_result.stdout.strip()
                        
                        environments.append({
                            "name": env_name,
                            "path": python_path,
                            "version": version
                        })
                    except Exception:
                        pass
    except Exception as e:
        print(f"Error getting conda environments: {e}", file=sys.stderr)
    
    return environments


def get_installed_packages(python_path: str) -> List[Dict[str, str]]:
    """Get installed packages for a specific Python environment."""
    try:
        result = subprocess.run(
            [python_path, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, check=True, timeout=30,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        return json.loads(result.stdout)
    except Exception as e:
        print(f"Error getting installed packages: {e}", file=sys.stderr)
        return []


def find_python_files(directory: Path) -> List[Dict[str, str]]:
    """Find all Python files in a directory."""
    files = []
    
    if not directory.exists():
        return files
    
    for path in directory.rglob("*.py"):
        if path.is_file():
            files.append({
                "path": str(path),
                "name": path.name,
                "size": path.stat().st_size,
                "modified": path.stat().st_mtime
            })
    
    return files


# ============================================================================
# Resources
# ============================================================================

@mcp.resource("python://environments")
def get_environments_resource() -> str:
    """List all available Python environments as a resource."""
    environments = get_python_environments()
    return json.dumps(environments, indent=2)


@mcp.resource("python://packages/{env_name}")
def get_packages_resource(env_name: str) -> str:
    """List installed packages for a specific environment as a resource."""
    environments = get_python_environments()
    
    env = next((e for e in environments if e["name"] == env_name), None)
    if not env:
        return json.dumps({"error": f"Environment '{env_name}' not found"})
    
    packages = get_installed_packages(env["path"])
    return json.dumps(packages, indent=2)


@mcp.resource("python://directory")
def get_working_directory_listing() -> str:
    """List all Python files in the working directory as a resource."""
    try:
        files = find_python_files(WORKING_DIR)
        return json.dumps({
            "working_directory": str(WORKING_DIR),
            "files": files
        }, indent=2)
    except Exception as e:
        return json.dumps({"error": f"Error listing directory: {str(e)}"})


@mcp.resource("python://session/{session_id}/history")
def get_session_history(session_id: str) -> str:
    """Get execution history for a REPL session."""
    if session_id not in _sessions:
        return json.dumps({"error": f"Session '{session_id}' not found"})
    
    session = _sessions[session_id]
    return json.dumps({
        "session_id": session_id,
        "history": session.history
    }, indent=2)


# ============================================================================
# Tools
# ============================================================================

@mcp.tool()
def list_python_environments() -> str:
    """List all available Python environments (system Python and conda environments)."""
    environments = get_python_environments()
    
    if not environments:
        return "No Python environments found."
    
    result = "Available Python Environments:\n\n"
    for env in environments:
        result += f"- Name: {env['name']}\n"
        result += f"  Path: {env['path']}\n"
        result += f"  Version: Python {env['version']}\n\n"
    
    return result


@mcp.tool()
def list_installed_packages(environment: str = "default") -> str:
    """
    List installed packages for a specific Python environment.
    
    Args:
        environment: Name of the Python environment
    """
    environments = get_python_environments()
    
    if environment == "default" and not any(e["name"] == "default" for e in environments):
        environment = "system"
    
    env = next((e for e in environments if e["name"] == environment), None)
    if not env:
        return f"Environment '{environment}' not found. Available: {', '.join(e['name'] for e in environments)}"
    
    packages = get_installed_packages(env["path"])
    
    if not packages:
        return f"No packages found in environment '{environment}'."
    
    result = f"Installed Packages in '{environment}':\n\n"
    for pkg in packages:
        result += f"- {pkg['name']} {pkg['version']}\n"
    
    return result


@mcp.tool()
async def run_python_code(
    code: str,
    execution_mode: str = "inline",
    session_id: str = "default",
    environment: str = "system",
    save_as: Optional[str] = None,
    timeout: int = 300
) -> str:
    """
    Execute Python code with flexible execution modes.
    
    Args:
        code: Python code to execute
        execution_mode: Execution mode - "inline" (default, fast, in-process) or "subprocess" (isolated)
        session_id: Session ID for inline mode to maintain state across executions
        environment: Python environment name (only for subprocess mode)
        save_as: Optional filename to save the code before execution
        timeout: Maximum execution time in seconds (only enforced for subprocess mode)
    
    Returns:
        Execution result with output
    
    Execution modes:
    - "inline" (default): Executes code in the current process. Fast and reliable,
      maintains session state. Use for most code execution tasks.
    - "subprocess": Executes code in a separate Python process. Use when you need
      environment isolation or a different Python environment.
    """
    
    # Save code if requested
    if save_as:
        save_path = WORKING_DIR / save_as
        if not save_path.suffix == '.py':
            save_path = save_path.with_suffix('.py')
            
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(code)
        except Exception as e:
            return f"Error saving code to file: {str(e)}"
    
    # Execute based on mode
    if execution_mode == "inline":
        # In-process execution (default, fast, no subprocess issues)
        try:
            session = get_session(session_id)
            result = session.execute(code, timeout)
            
            # Store in history
            session.history.append({
                "code": code,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
                "status": result["status"]
            })
            
            output = f"Execution in session '{session_id}' (inline mode)"
            if save_as:
                output += f" (saved to {save_as})"
            output += ":\n\n"
            
            if result["status"] == 0:
                output += "--- Output ---\n"
                output += result["stdout"] if result["stdout"] else "(No output)\n"
            else:
                output += "--- Error ---\n"
                output += result["stderr"] if result["stderr"] else "(No error message)\n"
                
                if result["stdout"]:
                    output += "\n--- Output ---\n"
                    output += result["stdout"]
            
            return output
            
        except Exception as e:
            return f"Error in inline execution: {str(e)}\n{traceback.format_exc()}"
    
    elif execution_mode == "subprocess":
        # Subprocess execution (for environment isolation)
        environments = get_python_environments()
        
        if environment == "default" and not any(e["name"] == "default" for e in environments):
            environment = "system"
            
        env = next((e for e in environments if e["name"] == environment), None)
        if not env:
            return f"Environment '{environment}' not found. Available: {', '.join(e['name'] for e in environments)}"
        
        result = await execute_python_code_subprocess(code, env["path"], str(WORKING_DIR), timeout)
        
        output = f"Execution in '{environment}' environment (subprocess mode)"
        if save_as:
            output += f" (saved to {save_as})"
        output += ":\n\n"
        
        if result["status"] == 0:
            output += "--- Output ---\n"
            output += result["stdout"] if result["stdout"] else "(No output)\n"
        else:
            output += f"--- Error (status code: {result['status']}) ---\n"
            output += result["stderr"] if result["stderr"] else "(No error message)\n"
            
            if result["stdout"]:
                output += "\n--- Output ---\n"
                output += result["stdout"]
        
        return output
    
    else:
        return f"Unknown execution mode: {execution_mode}. Use 'inline' or 'subprocess'."


@mcp.tool()
async def run_python_file(
    file_path: str,
    environment: str = "default",
    arguments: Optional[List[str]] = None,
    timeout: int = 300
) -> str:
    """
    Execute a Python file (always uses subprocess for file execution).
    
    Args:
        file_path: Path to the Python file to execute
        environment: Name of the Python environment to use
        arguments: List of command-line arguments to pass to the script
        timeout: Maximum execution time in seconds (default: 300)
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = WORKING_DIR / path
    if not is_path_allowed(path):
        return f"Access denied: Can only run files in working directory: {WORKING_DIR}"

    if not path.exists():
        return f"File '{path}' not found."
    
    environments = get_python_environments()
    
    if environment == "default" and not any(e["name"] == "default" for e in environments):
        environment = "system"
        
    env = next((e for e in environments if e["name"] == environment), None)
    if not env:
        return f"Environment '{environment}' not found. Available: {', '.join(e['name'] for e in environments)}"
    
    cmd = [env["path"], str(path)]
    if arguments:
        cmd.extend(arguments)
    
    result = await run_subprocess_async(cmd, cwd=str(WORKING_DIR), timeout=timeout)
    
    output = f"Execution of '{path}' in '{environment}' environment:\n\n"
    
    if result["status"] == 0:
        output += "--- Output ---\n"
        output += result["stdout"] if result["stdout"] else "(No output)\n"
    else:
        output += f"--- Error (status code: {result['status']}) ---\n"
        output += result["stderr"] if result["stderr"] else "(No error message)\n"
        
        if result["stdout"]:
            output += "\n--- Output ---\n"
            output += result["stdout"]
    
    return output


@mcp.tool()
async def install_package(
    package_name: str,
    environment: str = "default",
    upgrade: bool = False,
    timeout: int = 300
) -> str:
    """
    Install a Python package in the specified environment.
    
    Args:
        package_name: Name of the package to install
        environment: Name of the Python environment
        upgrade: Whether to upgrade if already installed
        timeout: Maximum execution time in seconds
    """
    environments = get_python_environments()
    
    if environment == "default" and not any(e["name"] == "default" for e in environments):
        environment = "system"
        
    env = next((e for e in environments if e["name"] == environment), None)
    if not env:
        return f"Environment '{environment}' not found. Available: {', '.join(e['name'] for e in environments)}"
    
    cmd = [env["path"], "-m", "pip", "install"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(package_name)
    
    result = await run_subprocess_async(cmd, timeout=timeout)
    
    if result["status"] == 0:
        return f"Successfully {'upgraded' if upgrade else 'installed'} {package_name} in {environment}."
    else:
        return f"Error installing {package_name}:\n{result['stderr']}"


@mcp.tool()
def read_file(file_path: str, max_size_kb: int = 1024) -> str:
    """
    Read the content of any file, with size limits for safety.
    
    Args:
        file_path: Path to the file
        max_size_kb: Maximum file size to read in KB
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = WORKING_DIR / path
    if not is_path_allowed(path):
        return f"Access denied: Can only read files in working directory: {WORKING_DIR}"
    
    try:
        if not path.exists():
            return f"Error: File '{file_path}' not found"
        
        file_size_kb = path.stat().st_size / 1024
        if file_size_kb > max_size_kb:
            return f"Error: File size ({file_size_kb:.2f} KB) exceeds maximum ({max_size_kb} KB)"
        
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            source_extensions = ['.py', '.js', '.html', '.css', '.json', '.xml', '.md', '.txt', '.sh', '.bat', '.ps1']
            if path.suffix.lower() in source_extensions:
                file_type = path.suffix[1:] if path.suffix else 'plain'
                return f"File: {file_path}\n\n```{file_type}\n{content}\n```"
            
            return f"File: {file_path}\n\n{content}"
        
        except UnicodeDecodeError:
            with open(path, 'rb') as f:
                content = f.read()
                hex_content = content.hex()
                return f"Binary file: {file_path}\nSize: {len(content)} bytes\nHex (first 1024 chars):\n{hex_content[:1024]}"
    
    except Exception as e:
        return f"Error reading file: {str(e)}"


@mcp.tool()
def write_file(
    file_path: str,
    content: str,
    overwrite: bool = False
) -> str:
    """
    Write content to a file.
    
    Args:
        file_path: Path to the file to write
        content: Content to write
        overwrite: Whether to overwrite if exists
    """
    path = Path(file_path)
    if not path.is_absolute():
        path = WORKING_DIR / path
    if not is_path_allowed(path):
        return f"Access denied: Can only write files in working directory: {WORKING_DIR}"
    
    try:
        if path.exists() and not overwrite:
            return f"File '{path}' exists. Use overwrite=True to replace."
        
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        
        file_size_kb = path.stat().st_size / 1024
        return f"Successfully wrote to {path}. Size: {file_size_kb:.2f} KB"
    
    except Exception as e:
        return f"Error writing file: {str(e)}"


@mcp.tool()
def list_directory(directory_path: str = "") -> str:
    """
    List all Python files in a directory.
    
    Args:
        directory_path: Path to directory (empty for working directory)
    """
    try:
        if not directory_path:
            path = WORKING_DIR
        else:
            path = Path(directory_path)
            if not path.is_absolute():
                path = WORKING_DIR / directory_path
            if not is_path_allowed(path):
                return f"Access denied: Can only list files in working directory: {WORKING_DIR}"
                
        if not path.exists():
            return f"Error: Directory '{directory_path}' not found"
            
        if not path.is_dir():
            return f"Error: '{directory_path}' is not a directory"
            
        files = find_python_files(path)
        
        if not files:
            return f"No Python files found in {directory_path or 'working directory'}"
            
        result = f"Python files in: {directory_path or str(WORKING_DIR)}\n\n"
        
        files_by_dir = {}
        base_dir = path if ALLOW_SYSTEM_ACCESS else WORKING_DIR
        
        for file in files:
            file_path = Path(file["path"])
            try:
                relative_path = file_path.relative_to(base_dir)
                parent = str(relative_path.parent)
                if parent == ".":
                    parent = "(root)"
            except ValueError:
                parent = str(file_path.parent)
                
            if parent not in files_by_dir:
                files_by_dir[parent] = []
                
            files_by_dir[parent].append({
                "name": file["name"],
                "size": file["size"]
            })
            
        for dir_name, dir_files in sorted(files_by_dir.items()):
            result += f"📁 {dir_name}:\n"
            for file in sorted(dir_files, key=lambda x: x["name"]):
                size_kb = round(file["size"] / 1024, 1)
                result += f"  📄 {file['name']} ({size_kb} KB)\n"
            result += "\n"
            
        return result
    except Exception as e:
        return f"Error listing directory: {str(e)}"


@mcp.tool()
def clear_session(session_id: str = "default") -> str:
    """
    Clear a REPL session's state and history.
    
    Args:
        session_id: Session ID to clear
    """
    if session_id in _sessions:
        del _sessions[session_id]
        return f"Session '{session_id}' cleared."
    else:
        return f"Session '{session_id}' not found."


@mcp.tool()
def list_sessions() -> str:
    """List all active REPL sessions."""
    if not _sessions:
        return "No active sessions."
    
    result = "Active REPL Sessions:\n\n"
    for session_id, session in _sessions.items():
        result += f"- Session: {session_id}\n"
        result += f"  History entries: {len(session.history)}\n"
        result += f"  Variables: {len([k for k in session.locals.keys() if not k.startswith('__')])}\n\n"
    
    return result


# ============================================================================
# Prompts
# ============================================================================

@mcp.prompt()
def python_function_template(description: str) -> str:
    """Generate a template for a Python function with docstring."""
    return f"""Please create a Python function based on this description:

{description}

Include:
- Type hints
- Docstring with parameters, return value, and examples
- Error handling where appropriate
- Comments for complex logic"""


@mcp.prompt()
def refactor_python_code(code: str) -> str:
    """Help refactor Python code for better readability and performance."""
    return f"""Please refactor this Python code to improve readability, performance, error handling, and structure:

```python
{code}
```

Explain the changes you made and why they improve the code."""


@mcp.prompt()
def debug_python_error(code: str, error_message: str) -> str:
    """Help debug a Python error."""
    return f"""I'm getting this error:

```python
{code}
```

Error message:
```
{error_message}
```

Please help by:
1. Explaining what the error means
2. Identifying the cause
3. Suggesting fixes"""


# Run the server
if __name__ == "__main__":
    # Delegate transport selection to main.py so `python server.py` and the
    # console-script entrypoint behave identically.
    from mcp_python_interpreter.main import main
    main()