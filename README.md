# thingkaton

This template contains a simple python app served by [fastapi](https://github.com/tiangolo/fastapi).
It shows you how to use the [NOVA Python SDK](https://github.com/wandelbotsgmbh/wandelbots-nova) and build a basic app with it.

## Controller Management System

The app features a resilient controller management system that:

- **Automatically discovers controllers** using `nova.cell().controllers()`
- **Dynamically starts state streaming** for each discovered controller
- **Handles controller removal** by stopping streaming for removed controllers
- **Detects new controllers** and automatically starts streaming for them
- **Recovers from failures** by restarting failed streaming tasks
- **Maintains separate state tracking** for each controller

### Key Features:

- **Resilient Architecture**: If one controller fails, others continue working independently
- **Dynamic Discovery**: Controllers are checked every 30 seconds for changes
- **Graceful Cleanup**: Proper resource cleanup when the application stops
- **Comprehensive Logging**: Detailed logs for monitoring and debugging

Use the following steps for development:

* make sure you have `uv` installed
    * you can follow these steps https://docs.astral.sh/uv/getting-started/installation/
* ensure proper environment variables are set in `.env`
    * note: you might need to set/update `NOVA_ACCESS_TOKEN` and `NOVA_API`
* use `uv run python -m thingkaton` to run the the server
    * access the example on `http://localhost:3000`
* build, push and install the app with `nova app install`

## formatting

```bash
$ ruff format
$ ruff check --select I --fix
```