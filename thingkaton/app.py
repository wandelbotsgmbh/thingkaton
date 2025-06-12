from thingkaton.wakurobotics.care.devices.v1.error_schema import DeviceErrors, Error
from thingkaton.wakurobotics.care.devices.v1.factsheet_schema import DeviceFactsheet
from thingkaton.wakurobotics.care.client import Client, get_timestamp


from decouple import config
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from loguru import logger
import asyncio
from contextlib import asynccontextmanager
from nova import Controller, Nova
from nova import MotionSettings, Nova
from nova.actions import cartesian_ptp, joint_ptp
from nova.api import models
from nova.cell import virtual_controller
from nova.types import Pose
import traceback
import wandelbots_api_client as wb

from thingkaton.wakurobotics.care.devices.v1.order_schema import DeviceOrder
import uuid


waku_client_password = config("WAKU_CLIENT_PASSWORD", cast=str)




async def report_safety_state(robot_controller_state: wb.models.RobotControllerState, waku_client: Client):
    current_safety_state = robot_controller_state.safety_state
    if current_safety_state not in [
        "SAFETY_STATE_ROBOT_EMERGENCY_STOP"
    ]:
        return
        

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
    logger.info(f"Reporting safety state: {current_safety_state}")
    waku_client.publish_device_errors("ur", device_errors)


async def sync_device_state_to_waku():
    previous_safety_state = None

    waku_client = await get_waku_client()
    while True:
        try:
            async with Nova() as nova:
                print("test")
                cell = nova.cell()
                controllers = await cell.controllers()
                controller = await cell.controller("ur")
                await register_waku_device(waku_client, controller)

                state_generator = nova._api_client.controller_api.stream_robot_controller_state("cell", "ur", 200)
                print(controllers)
                async for state in state_generator:
                    state: wb.models.RobotControllerState = state
                    current_safety_state = state.safety_state
                    
                    # Only print when safety state changes
                    if current_safety_state != previous_safety_state:
                        print(f"Safety state changed: {previous_safety_state} -> {current_safety_state}")
                        previous_safety_state = current_safety_state
                        await report_safety_state(state, waku_client)


        except asyncio.CancelledError:
            logger.info("Background task cancelled, exiting sync loop.")
            break
        except Exception as e:
            logger.error(f"Error syncing device state to Waku: {e}")
            traceback.print_stack()
            await asyncio.sleep(5)  # Wait before retrying on error

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



@app.post(
    "/push_data",
    status_code=200,
    summary="Add your API call here",
    description="This is a placeholder for your API call.",
)
async def push_data():
    publisher = await get_waku_client()

    async with Nova() as nova:
        cell = nova.cell()
        controller = await cell.ensure_controller(
            robot_controller=virtual_controller(
                name="ur",
                manufacturer=models.Manufacturer.UNIVERSALROBOTS,
                type=models.VirtualControllerTypes.UNIVERSALROBOTS_MINUS_UR3E,
            )
        )
        await register_waku_device(publisher, controller)
        cycle_id = str(uuid.uuid4())
        publisher.publish_device_order(controller.id, DeviceOrder(timestamp=get_timestamp(),id=cycle_id, status="started"))

        # Connect to the controller and activate motion groups
        async with controller[0] as motion_group:
            home_joints = await motion_group.joints()
            tcp_names = await motion_group.tcp_names()
            tcp = tcp_names[0]

            # Get current TCP pose and offset it slightly along the x-axis
            current_pose = await motion_group.tcp_pose(tcp)
            target_pose = current_pose @ Pose((1, 0, 0, 0, 0, 0))

            actions = [
                joint_ptp(home_joints),
                cartesian_ptp(target_pose),
                joint_ptp(home_joints),
                cartesian_ptp(target_pose @ [50, 0, 0, 0, 0, 0]),
                joint_ptp(home_joints),
                cartesian_ptp(target_pose @ (50, 100, 0, 0, 0, 0)),
                joint_ptp(home_joints),
                cartesian_ptp(target_pose @ Pose((0, 50, 0, 0, 0, 0))),
                joint_ptp(home_joints),
            ]

        joint_trajectory = await motion_group.plan(actions, tcp)
        motion_iter = motion_group.stream_execute(joint_trajectory, tcp, actions=actions)
        async for motion_state in motion_iter:
            print(motion_state)
        
        publisher.publish_device_order(controller.id, DeviceOrder(timestamp=get_timestamp(), id=cycle_id, status="finished", parameters={}))



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


async def register_waku_device(publisher: Client, controller: Controller):
    publisher.register_device(
        serial=controller.id,
        device_values=DeviceFactsheet(
            # put controller id here
            serial=controller.id,
            name=f"Wandelbots - {controller.name}",
            # map to the vandelbots controller data
            manufacturer="universal-robots",
            model="ur3e",
            version="1.0.0",
            deployment="Default",
        )
    )

    publisher.connect_device(serial=controller.id)