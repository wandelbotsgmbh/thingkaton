from thingkaton.wakurobotics.care.devices.v1.error_schema import DeviceErrors, Error
from thingkaton.wakurobotics.care.devices.v1.factsheet_schema import DeviceFactsheet
from thingkaton.wakurobotics.care.client import Client, get_timestamp

from decouple import config
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from loguru import logger
import asyncio
from contextlib import asynccontextmanager
from nova import Controller, Nova
import traceback
import wandelbots_api_client as wb
from pydantic import BaseModel
from typing import List, Optional




# TODO: there is too many open nova connections that we need to close.

waku_client_password = config("WAKU_CLIENT_PASSWORD", cast=str)




async def report_safety_state(robot_controller_state: wb.models.RobotControllerState, waku_client: Client, controller_id: str):
    current_safety_state = robot_controller_state.safety_state
    if current_safety_state not in [
        "SAFETY_STATE_ROBOT_EMERGENCY_STOP",
        "SAFETY_STATE_NORMAL"
    ]:
        return
        

    if current_safety_state == "SAFETY_STATE_ROBOT_EMERGENCY_STOP":
        waku_error = Error(
            title="Robot Controller Safety State",
            code=current_safety_state,
            description="Safety state of the robot controller has changed.",
            component="robot_controller",
            severity=4,
        )
        device_errors = DeviceErrors(
            timestamp=get_timestamp(),
            activeErrors=[waku_error]
        )
        logger.info(f"Reporting safety state for {controller_id}: {current_safety_state}")
        waku_client.publish_device_errors(controller_id, device_errors)


    if current_safety_state == "SAFETY_STATE_NORMAL":
        device_errors = DeviceErrors(
            timestamp=get_timestamp(),
            activeErrors=[]
        )
        logger.info(f"Reporting safety state for {controller_id}: {current_safety_state}")
        waku_client.publish_device_errors(controller_id, device_errors)



def map_robot_controller_to_waku_device(controller: wb.models.RobotController) -> DeviceFactsheet:
    """Map Wandelbots controller to Waku device factsheet"""
    
    manufacturer = None
    kind = None
    if isinstance(controller.configuration.actual_instance, wb.models.AbbController):
        manufacturer = "abb"
        kind = controller.configuration.actual_instance.kind.lower()
    elif isinstance(controller.configuration.actual_instance, wb.models.FanucController):
        manufacturer = "fanuc"
        kind = controller.configuration.actual_instance.kind.lower()
    elif isinstance(controller.configuration.actual_instance, wb.models.KukaController):
        manufacturer = "kuka"
        kind = controller.configuration.actual_instance.kind.lower()
    elif isinstance(controller.configuration.actual_instance, wb.models.UniversalrobotsController):
        manufacturer = "universal-robots"
        kind = controller.configuration.actual_instance.kind.lower()
    elif isinstance(controller.configuration.actual_instance, wb.models.YaskawaController):
        manufacturer = "yaskawa"
        kind = controller.configuration.actual_instance.kind.lower()
    elif isinstance(controller.configuration.actual_instance, wb.models.VirtualController):
        actual_instance: wb.models.VirtualController = controller.configuration.actual_instance
        if actual_instance.manufacturer.lower() == "universalrobots":
            manufacturer = "universal-robots"
        else:
            manufacturer = actual_instance.manufacturer.lower()

        result = actual_instance.type.split("-")[-1]
        kind = result.replace(" ", "-").replace("_", "-").lower()
    
    logger.info(f"Mapping controller {controller.name} to Waku device: {manufacturer} {kind}")


    device_fact_sheet=DeviceFactsheet(
            # put controller id here
            serial=controller.name,
            name=f"Wandelbots Nova Cloud - {controller.name} - {manufacturer} {kind}",
            # map to the vandelbots controller data
            manufacturer=manufacturer,
            model=kind,
            version="1.0.0",
            deployment="Default",
    )
    return device_fact_sheet



class ControllerManager:
    """Manages state streaming for multiple controllers dynamically"""
    
    def __init__(self):
        self.active_controllers = {}  # controller_id -> task
        self.waku_client = None
        self.nova = None
        self.cell = None
        self.controller_states = {}  # controller_id -> previous_safety_state
        
    async def initialize(self):
        """Initialize the controller manager"""
        ## TODO: don't connect all the time
        self.waku_client = await get_waku_client()
        self.nova = Nova()
        await self.nova.__aenter__()
        self.cell = self.nova.cell()
        
    async def cleanup(self):
        """Clean up resources"""
        # Cancel all active controller tasks
        for controller_id, task in self.active_controllers.items():
            if not task.cancelled():
                logger.info(f"Cancelling state streaming for controller: {controller_id}")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        
        self.active_controllers.clear()
        
        # Clean up Nova connection
        if self.nova:
            await self.nova.__aexit__(None, None, None)
            
    async def stream_controller_state(self, controller_id: str):
        """Stream state for a specific controller"""
        logger.info(f"Starting state streaming for controller: {controller_id}")
        
        try:
            # find model name to pass to waku
            # there is a better way of getting this information from API, for now leaving as it
            controller_configuration = await self.nova._api_client.controller_api.get_robot_controller(cell="cell", controller=controller_id)
            devicefact_sheet = map_robot_controller_to_waku_device(controller_configuration)
            await register_waku_device(
                self.waku_client,
                devicefact_sheet
            )
            
            state_generator = self.nova._api_client.controller_api.stream_robot_controller_state(
                "cell", controller_id, 200
            )
            
            previous_safety_state = self.controller_states.get(controller_id)
            
            async for state in state_generator:
                state: wb.models.RobotControllerState = state
                current_safety_state = state.safety_state
                
                # Only print when safety state changes
                if current_safety_state != previous_safety_state:
                    logger.info(f"Controller {controller_id} safety state changed: {previous_safety_state} -> {current_safety_state}")
                    previous_safety_state = current_safety_state
                    self.controller_states[controller_id] = current_safety_state
                    await report_safety_state(state, self.waku_client, controller_id)
                    
        except asyncio.CancelledError:
            logger.info(f"State streaming cancelled for controller: {controller_id}")
            raise
        except Exception as e:
            # TODO should we handle connection errors differently?
            logger.error(f"Error streaming state for controller {controller_id}: {e}")
            traceback.print_exc()
            raise
            
    async def start_controller_streaming(self, controller_id: str):
        """Start streaming for a specific controller"""
        if controller_id in self.active_controllers:
            logger.debug(f"Controller {controller_id} already has active streaming")
            return
            
        task = asyncio.create_task(self.stream_controller_state(controller_id))
        self.active_controllers[controller_id] = task
        logger.info(f"Started streaming for controller: {controller_id}")
        
    async def stop_controller_streaming(self, controller_id: str):
        """Stop streaming for a specific controller"""
        if controller_id not in self.active_controllers:
            logger.debug(f"Controller {controller_id} not found in active controllers")
            return
            
        task = self.active_controllers.pop(controller_id)
        if not task.cancelled():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        
        # Clean up state
        if controller_id in self.controller_states:
            del self.controller_states[controller_id]
            
        logger.info(f"Stopped streaming for controller: {controller_id}")
        
    async def discover_and_manage_controllers(self):
        """Discover controllers and manage their streaming states"""
        try:
            # Get current controllers
            controllers = await self.cell.controllers()
            current_controller_ids = {controller.id for controller in controllers}
            
            logger.debug(f"Discovered controllers: {current_controller_ids}")
            
            # Find new controllers to start streaming
            new_controllers = current_controller_ids - set(self.active_controllers.keys())
            for controller_id in new_controllers:
                logger.info(f"New controller detected: {controller_id}")
                await self.start_controller_streaming(controller_id)
                
            # Find removed controllers to stop streaming
            removed_controllers = set(self.active_controllers.keys()) - current_controller_ids
            for controller_id in removed_controllers:
                logger.info(f"Controller removed: {controller_id}")
                await self.stop_controller_streaming(controller_id)
                
            # Check for completed/failed tasks and restart them
            completed_tasks = []
            for controller_id, task in self.active_controllers.items():
                if task.done():
                    completed_tasks.append(controller_id)
                    
            for controller_id in completed_tasks:
                logger.warning(f"Controller {controller_id} task completed unexpectedly, restarting...")
                await self.stop_controller_streaming(controller_id)
                if controller_id in current_controller_ids:
                    await self.start_controller_streaming(controller_id)
                    
        except Exception as e:
            logger.error(f"Error in controller discovery: {e}")
            traceback.print_exc()


# Pydantic models for API responses
class ControllerStatus(BaseModel):
    controller_id: str
    is_streaming: bool
    safety_state: Optional[str]
    task_status: str  # "running", "completed", "failed", "cancelled"

class SystemStatus(BaseModel):
    is_initialized: bool
    total_controllers: int
    active_streams: int
    discovered_controllers: List[str]
    active_controllers: List[str]
    system_health: str  # "healthy", "degraded", "unhealthy"

class ControllerResponse(BaseModel):
    success: bool
    message: str
    controller_id: Optional[str] = None

# Global controller manager instance for API access
global_controller_manager: Optional[ControllerManager] = None


async def sync_device_state_to_waku():
    """Main function to sync device states to Waku with resilient controller management"""
    global global_controller_manager
    global_controller_manager = ControllerManager()
    
    try:
        await global_controller_manager.initialize()
        logger.info("Controller manager initialized successfully")
        
        # Discovery loop - check for new/removed controllers every 30 seconds
        while True:
            try:
                await global_controller_manager.discover_and_manage_controllers()
                await asyncio.sleep(30)  # Check for controller changes every 30 seconds
                
            except asyncio.CancelledError:
                logger.info("Controller discovery loop cancelled")
                break
            except Exception as e:
                logger.error(f"Error in controller discovery loop: {e}")
                await asyncio.sleep(5)  # Wait before retrying on error
                
    except asyncio.CancelledError:
        logger.info("Background task cancelled, cleaning up...")
    except Exception as e:
        logger.error(f"Fatal error in sync_device_state_to_waku: {e}")
        traceback.print_exc()
    finally:
        await global_controller_manager.cleanup()
        global_controller_manager = None
        logger.info("Controller manager cleaned up")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for FastAPI application"""
    # Startup
    logger.info("Starting background task...")
    background_task = asyncio.create_task(sync_device_state_to_waku())
    
    yield
    
    # Shutdown
    if background_task:
        logger.info("Stopping background task...")
        background_task.cancel()
        try:
            await background_task
        except asyncio.CancelledError:
            logger.info("Background task cancelled successfully")


CELL_ID = config("CELL_ID", default="cell", cast=str)
BASE_PATH = config("BASE_PATH", default="", cast=str)
app = FastAPI(title="thingkaton", root_path=BASE_PATH, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# TODO: when starting with uv run python -m thingkaton it doesn't read correct .env variables

# Management API Endpoints

@app.get("/api/system/status", response_model=SystemStatus, summary="Get system status")
async def get_system_status():
    """Get the current status of the controller management system"""
    if not global_controller_manager:
        return SystemStatus(
            is_initialized=False,
            total_controllers=0,
            active_streams=0,
            discovered_controllers=[],
            active_controllers=[],
            system_health="unhealthy"
        )
    
    try:
        # Get discovered controllers
        controllers = await global_controller_manager.cell.controllers() if global_controller_manager.cell else []
        discovered_controller_ids = [controller.id for controller in controllers]
        
        active_controller_ids = list(global_controller_manager.active_controllers.keys())
        
        # Determine system health
        health = "healthy"
        if not global_controller_manager.nova or not global_controller_manager.waku_client:
            health = "unhealthy"
        elif len(active_controller_ids) < len(discovered_controller_ids):
            health = "degraded"
        
        return SystemStatus(
            is_initialized=bool(global_controller_manager.nova and global_controller_manager.waku_client),
            total_controllers=len(discovered_controller_ids),
            active_streams=len(active_controller_ids),
            discovered_controllers=discovered_controller_ids,
            active_controllers=active_controller_ids,
            system_health=health
        )
    except Exception as e:
        logger.error(f"Error getting system status: {e}")
        return SystemStatus(
            is_initialized=False,
            total_controllers=0,
            active_streams=0,
            discovered_controllers=[],
            active_controllers=[],
            system_health="unhealthy"
        )

@app.get("/api/controllers", response_model=List[ControllerStatus], summary="List all controllers")
async def list_controllers():
    """List all discovered controllers and their streaming status"""
    if not global_controller_manager or not global_controller_manager.cell:
        return []
    
    try:
        controllers = await global_controller_manager.cell.controllers()
        controller_statuses = []
        
        for controller in controllers:
            task = global_controller_manager.active_controllers.get(controller.id)
            task_status = "not_streaming"
            
            if task:
                if task.cancelled():
                    task_status = "cancelled"
                elif task.done():
                    if task.exception():
                        task_status = "failed"
                    else:
                        task_status = "completed"
                else:
                    task_status = "running"
            
            safety_state = global_controller_manager.controller_states.get(controller.id)
            
            controller_statuses.append(ControllerStatus(
                controller_id=controller.id,
                is_streaming=controller.id in global_controller_manager.active_controllers,
                safety_state=safety_state,
                task_status=task_status
            ))
        
        return controller_statuses
    except Exception as e:
        logger.error(f"Error listing controllers: {e}")
        raise HTTPException(status_code=500, detail=f"Error listing controllers: {str(e)}")

@app.post("/api/controllers/{controller_id}/start", response_model=ControllerResponse, summary="Start controller streaming")
async def start_controller_streaming(controller_id: str):
    """Start streaming for a specific controller"""
    if not global_controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")
    
    try:
        if controller_id in global_controller_manager.active_controllers:
            return ControllerResponse(
                success=False,
                message=f"Controller {controller_id} is already streaming",
                controller_id=controller_id
            )
        
        await global_controller_manager.start_controller_streaming(controller_id)
        return ControllerResponse(
            success=True,
            message=f"Started streaming for controller {controller_id}",
            controller_id=controller_id
        )
    except Exception as e:
        logger.error(f"Error starting controller streaming: {e}")
        return ControllerResponse(
            success=False,
            message=f"Failed to start streaming: {str(e)}",
            controller_id=controller_id
        )

@app.post("/api/controllers/{controller_id}/stop", response_model=ControllerResponse, summary="Stop controller streaming")
async def stop_controller_streaming(controller_id: str):
    """Stop streaming for a specific controller"""
    if not global_controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")
    
    try:
        if controller_id not in global_controller_manager.active_controllers:
            return ControllerResponse(
                success=False,
                message=f"Controller {controller_id} is not currently streaming",
                controller_id=controller_id
            )
        
        await global_controller_manager.stop_controller_streaming(controller_id)
        return ControllerResponse(
            success=True,
            message=f"Stopped streaming for controller {controller_id}",
            controller_id=controller_id
        )
    except Exception as e:
        logger.error(f"Error stopping controller streaming: {e}")
        return ControllerResponse(
            success=False,
            message=f"Failed to stop streaming: {str(e)}",
            controller_id=controller_id
        )

@app.post("/api/controllers/{controller_id}/restart", response_model=ControllerResponse, summary="Restart controller streaming")
async def restart_controller_streaming(controller_id: str):
    """Restart streaming for a specific controller"""
    if not global_controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")
    
    try:
        # Stop first if running
        if controller_id in global_controller_manager.active_controllers:
            await global_controller_manager.stop_controller_streaming(controller_id)
        
        # Start streaming
        await global_controller_manager.start_controller_streaming(controller_id)
        return ControllerResponse(
            success=True,
            message=f"Restarted streaming for controller {controller_id}",
            controller_id=controller_id
        )
    except Exception as e:
        logger.error(f"Error restarting controller streaming: {e}")
        return ControllerResponse(
            success=False,
            message=f"Failed to restart streaming: {str(e)}",
            controller_id=controller_id
        )

@app.get("/api/controllers/{controller_id}/status", response_model=ControllerStatus, summary="Get controller status")
async def get_controller_status(controller_id: str):
    """Get detailed status for a specific controller"""
    if not global_controller_manager or not global_controller_manager.cell:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")
    
    try:
        # Check if controller exists
        controllers = await global_controller_manager.cell.controllers()
        controller = next((c for c in controllers if c.id == controller_id), None)
        
        if controller is None:
            raise HTTPException(status_code=404, detail=f"Controller {controller_id} not found")
        
        task = global_controller_manager.active_controllers.get(controller_id)
        task_status = "not_streaming"
        
        if task:
            if task.cancelled():
                task_status = "cancelled"
            elif task.done():
                if task.exception():
                    task_status = "failed"
                else:
                    task_status = "completed"
            else:
                task_status = "running"
        
        safety_state = global_controller_manager.controller_states.get(controller_id)
        
        return ControllerStatus(
            controller_id=controller.id,
            is_streaming=controller_id in global_controller_manager.active_controllers,
            safety_state=safety_state,
            task_status=task_status
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting controller status: {e}")
        raise HTTPException(status_code=500, detail=f"Error getting controller status: {str(e)}")

@app.post("/api/system/discover", summary="Trigger controller discovery")
async def trigger_discovery():
    """Manually trigger controller discovery and management"""
    if not global_controller_manager:
        raise HTTPException(status_code=503, detail="Controller manager not initialized")
    
    try:
        await global_controller_manager.discover_and_manage_controllers()
        return {"success": True, "message": "Controller discovery completed"}
    except Exception as e:
        logger.error(f"Error during manual discovery: {e}")
        raise HTTPException(status_code=500, detail=f"Discovery failed: {str(e)}")

# Existing endpoints

@app.get("/", summary="Opens the Stoplight UI", response_class=HTMLResponse)
async def root():
    # One could serve a nice UI here as well. For simplicity, we just redirect to the Stoplight UI.
    return f"""
    <!doctype html>
        <html lang="en">
          <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
            <title>Elements in HTML</title>
            <!-- Embed elements Elements via Web Component -->
            <script src="https://unpkg.com/@stoplight/elements/web-components.min.js"></script>
            <link rel="stylesheet" href="https://unpkg.com/@stoplight/elements/styles.min.css">
          </head>
          <body>

            <elements-api
              apiDescriptionUrl="{BASE_PATH}/openapi.json"
              router="hash"
              layout="sidebar"
              tryItCredentialsPolicy="same-origin"
            />

          </body>
    </html>
    """


@app.get("/app_icon.png", summary="Services the app icon for the homescreen")
async def get_app_icon():
    try:
        return FileResponse(path="static/app_icon.png", media_type="image/png")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Icon not found")


async def get_waku_client() -> Client:
    publisher = Client(
        customer_id="manufacturingx",
        connection_id="wandelbots",
        broker="mqtt.waku-robotics.com",
        port=8883,
        username="wandelbots",
        password=waku_client_password
    )
    publisher.connect()
    return publisher


async def register_waku_device(publisher: Client, device_fact_sheet: DeviceFactsheet):
    publisher.register_device(
        serial=device_fact_sheet.serial,
        device_values=device_fact_sheet
    )

    publisher.connect_device(serial=device_fact_sheet.serial)