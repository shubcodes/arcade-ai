import asyncio
import os
import threading
import uuid
import webbrowser
from pathlib import Path
from typing import Any, Optional

import httpx
import typer
from arcadepy import Arcade
from arcadepy.types import AuthorizationResponse
from openai import OpenAI, OpenAIError
from rich.console import Console
from rich.markup import escape
from rich.text import Text
from tqdm import tqdm

import arcade.cli.worker as worker
from arcade.cli.authn import LocalAuthCallbackServer, check_existing_login
from arcade.cli.constants import (
    CREDENTIALS_FILE_PATH,
    LOCALHOST,
    PROD_CLOUD_HOST,
    PROD_ENGINE_HOST,
)
from arcade.cli.display import (
    display_arcade_chat_header,
    display_eval_results,
    display_tool_messages,
)
from arcade.cli.launcher import start_servers
from arcade.cli.show import show_logic
from arcade.cli.utils import (
    OrderCommands,
    compute_base_url,
    compute_login_url,
    get_eval_files,
    get_today_context,
    get_user_input,
    handle_chat_interaction,
    handle_tool_authorization,
    handle_user_command,
    is_authorization_pending,
    load_eval_suites,
    log_engine_health,
    validate_and_get_config,
    version_callback,
)
from arcade.cli.worker import parse_deployment_response
from arcade.worker.config.deployment import Deployment

cli = typer.Typer(
    cls=OrderCommands,
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    pretty_exceptions_show_locals=False,
    pretty_exceptions_short=True,
    rich_markup_mode="markdown",
)


cli.add_typer(worker.app, name="worker", help="Manage workers")
console = Console()


@cli.command(help="Log in to Arcade Cloud", rich_help_panel="User")
def login(
    host: str = typer.Option(
        PROD_CLOUD_HOST,
        "-h",
        "--host",
        help="The Arcade Cloud host to log in to.",
    ),
    port: Optional[int] = typer.Option(
        None,
        "-p",
        "--port",
        help="The port of the Arcade Cloud host (if running locally).",
    ),
) -> None:
    """
    Logs the user into Arcade Cloud.
    """

    if check_existing_login():
        console.print("\nTo log out and delete your locally-stored credentials, use ", end="")
        console.print("arcade logout", style="bold green", end="")
        console.print(".\n")
        return

    # Start the HTTP server in a new thread
    state = str(uuid.uuid4())
    auth_server = LocalAuthCallbackServer(state)
    server_thread = threading.Thread(target=auth_server.run_server)
    server_thread.start()

    try:
        # Open the browser for user login
        login_url = compute_login_url(host, state, port)

        console.print("Opening a browser to log you in...")
        if not webbrowser.open(login_url):
            console.print(
                f"If a browser doesn't open automatically, copy this URL and paste it into your browser: {login_url}",
                style="dim",
            )

        # Wait for the server thread to finish
        server_thread.join()
    except KeyboardInterrupt:
        auth_server.shutdown_server()
    finally:
        if server_thread.is_alive():
            server_thread.join()  # Ensure the server thread completes and cleans up


@cli.command(help="Log out of Arcade Cloud", rich_help_panel="User")
def logout() -> None:
    """
    Logs the user out of Arcade Cloud.
    """
    # If the credentials file exists, delete it
    if os.path.exists(CREDENTIALS_FILE_PATH):
        os.remove(CREDENTIALS_FILE_PATH)
        console.print("You're now logged out.", style="bold")
    else:
        console.print("You're not logged in.", style="bold red")


@cli.command(help="Create a new toolkit package directory", rich_help_panel="Tool Development")
def new(
    directory: str = typer.Option(os.getcwd(), "--dir", help="tools directory path"),
) -> None:
    """
    Creates a new toolkit with the given name, description, and result type.
    """
    from arcade.cli.new import create_new_toolkit

    try:
        create_new_toolkit(directory)
    except Exception as e:
        error_message = f"❌ Failed to create new Toolkit: {escape(str(e))}"
        console.print(error_message, style="bold red")


@cli.command(
    help="Show the installed toolkits or details of a specific tool",
    rich_help_panel="Tool Development",
)
def show(
    toolkit: Optional[str] = typer.Option(
        None, "-T", "--toolkit", help="The toolkit to show the tools of"
    ),
    tool: Optional[str] = typer.Option(
        None, "-t", "--tool", help="The specific tool to show details for"
    ),
    host: str = typer.Option(
        PROD_ENGINE_HOST,
        "-h",
        "--host",
        help="The Arcade Engine address to show the tools/toolkits of.",
    ),
    local: bool = typer.Option(
        False,
        "--local",
        "-l",
        help="Show the local environment's catalog instead of an Arcade Engine's catalog.",
    ),
    port: Optional[int] = typer.Option(
        None,
        "-p",
        "--port",
        help="The port of the Arcade Engine.",
    ),
    force_tls: bool = typer.Option(
        False,
        "--tls",
        help="Whether to force TLS for the connection to the Arcade Engine. If not specified, the connection will use TLS if the engine URL uses a 'https' scheme.",
    ),
    force_no_tls: bool = typer.Option(
        False,
        "--no-tls",
        help="Whether to disable TLS for the connection to the Arcade Engine.",
    ),
    debug: bool = typer.Option(False, "--debug", "-d", help="Show debug information"),
) -> None:
    """
    Show the available toolkits or detailed information about a specific tool.
    """
    show_logic(toolkit, tool, host, local, port, force_tls, force_no_tls, debug)


@cli.command(help="Start Arcade Chat in the terminal", rich_help_panel="Launch")
def chat(
    model: str = typer.Option("gpt-4o", "-m", "--model", help="The model to use for prediction."),
    stream: bool = typer.Option(
        False, "-s", "--stream", is_flag=True, help="Stream the tool output."
    ),
    prompt: str = typer.Option(None, "--prompt", help="The system prompt to use for the chat."),
    debug: bool = typer.Option(False, "--debug", "-d", help="Show debug information"),
    host: str = typer.Option(
        PROD_ENGINE_HOST,
        "-h",
        "--host",
        help="The Arcade Engine address to send chat requests to.",
    ),
    port: int = typer.Option(
        None,
        "-p",
        "--port",
        help="The port of the Arcade Engine.",
    ),
    force_tls: bool = typer.Option(
        False,
        "--tls",
        help="Whether to force TLS for the connection to the Arcade Engine. If not specified, the connection will use TLS if the engine URL uses a 'https' scheme.",
    ),
    force_no_tls: bool = typer.Option(
        False,
        "--no-tls",
        help="Whether to disable TLS for the connection to the Arcade Engine.",
    ),
) -> None:
    """
    Chat with a language model.
    """
    try:
        import readline
    except ImportError:
        console.print(
            "Readline is not available on this platform. Command history will be limited.",
            style="dim",
        )

    config = validate_and_get_config()
    base_url = compute_base_url(force_tls, force_no_tls, host, port)

    client = Arcade(api_key=config.api.key, base_url=base_url)
    user_email = config.user.email if config.user else None

    try:
        # start messages conversation
        history: list[dict[str, Any]] = []

        # Ground the LLM with today's date and day of the week to help when calling date-related tools
        # in case the user refers to relative dates (e.g. next Monday, last month, etc)
        today_context = get_today_context()

        if prompt:
            prompt = f"{today_context} {prompt}"
        else:
            prompt = today_context

            history.append({"role": "system", "content": prompt})

        display_arcade_chat_header(base_url, stream)

        # Try to hit /health endpoint on engine and warn if it is down
        log_engine_health(client)

        while True:
            console.print(
                f"\n[magenta][bold]User[/bold] {user_email}: [/magenta]"
                + "([bold][default]/?[/default][/bold] for help)"
            )

            user_input = get_user_input()

            # Add the input to history
            readline.add_history(user_input)

            if handle_user_command(
                user_input, history, host, port, force_tls, force_no_tls, show_logic
            ):
                continue

            history.append({"role": "user", "content": user_input})

            try:
                # TODO fixup configuration to remove this + "/v1" workaround
                openai_client = OpenAI(api_key=config.api.key, base_url=base_url + "/v1")
                chat_result = handle_chat_interaction(
                    openai_client, model, history, user_email, stream
                )

                history = chat_result.history
                tool_messages = chat_result.tool_messages
                tool_authorization = chat_result.tool_authorization

                # wait for tool authorizations to complete, if any
                if tool_authorization and is_authorization_pending(tool_authorization):
                    chat_result = handle_tool_authorization(
                        client,
                        AuthorizationResponse.model_validate(tool_authorization),
                        history,
                        openai_client,
                        model,
                        user_email,
                        stream,
                    )
                    history = chat_result.history
                    tool_messages = chat_result.tool_messages

            except OpenAIError as e:
                console.print(f"❌ Arcade Chat failed with error: {e!s}", style="bold red")
                continue
            if debug:
                display_tool_messages(tool_messages)

    except KeyboardInterrupt:
        console.print("Chat stopped by user.", style="bold blue")
        typer.Exit()

    except RuntimeError as e:
        error_message = f"❌ Failed to run tool{': ' + escape(str(e)) if str(e) else ''}"
        console.print(error_message, style="bold red")
        raise typer.Exit()


@cli.command(help="Run tool calling evaluations", rich_help_panel="Tool Development")
def evals(
    directory: str = typer.Argument(".", help="Directory containing evaluation files"),
    show_details: bool = typer.Option(False, "--details", "-d", help="Show detailed results"),
    max_concurrent: int = typer.Option(
        1,
        "--max-concurrent",
        "-c",
        help="Maximum number of concurrent evaluations (default: 1)",
    ),
    models: str = typer.Option(
        "gpt-4o",
        "--models",
        "-m",
        help="The models to use for evaluation (default: gpt-4o). Use commas to separate multiple models.",
    ),
    host: str = typer.Option(
        LOCALHOST,
        "-h",
        "--host",
        help="The Arcade Engine address to send chat requests to.",
    ),
    cloud: bool = typer.Option(
        False,
        "--cloud",
        help="Whether to run evaluations against the Arcade Cloud Engine. Overrides the 'host' option.",
    ),
    port: int = typer.Option(
        None,
        "-p",
        "--port",
        help="The port of the Arcade Engine.",
    ),
    force_tls: bool = typer.Option(
        False,
        "--tls",
        help="Whether to force TLS for the connection to the Arcade Engine. If not specified, the connection will use TLS if the engine URL uses a 'https' scheme.",
    ),
    force_no_tls: bool = typer.Option(
        False,
        "--no-tls",
        help="Whether to disable TLS for the connection to the Arcade Engine.",
    ),
) -> None:
    """
    Find all files starting with 'eval_' in the given directory,
    execute any functions decorated with @tool_eval, and display the results.
    """
    config = validate_and_get_config()

    host = PROD_ENGINE_HOST if cloud else host
    base_url = compute_base_url(force_tls, force_no_tls, host, port)

    models_list = models.split(",")  # Use 'models_list' to avoid shadowing

    eval_files = get_eval_files(directory)
    if not eval_files:
        return

    console.print(
        Text.assemble(
            ("\nRunning evaluations against Arcade Engine at ", "bold"),
            (base_url, "bold blue"),
        )
    )

    # Try to hit /health endpoint on engine and warn if it is down
    with Arcade(api_key=config.api.key, base_url=base_url) as client:
        log_engine_health(client)

    # Use the new function to load eval suites
    eval_suites = load_eval_suites(eval_files)

    if not eval_suites:
        console.print("No evaluation suites to run.", style="bold yellow")
        return

    if show_details:
        suite_label = "suite" if len(eval_suites) == 1 else "suites"
        console.print(
            f"\nFound {len(eval_suites)} {suite_label} in the evaluation files.",
            style="bold",
        )

    async def run_evaluations() -> None:
        all_evaluations = []
        tasks = []
        for suite_func in eval_suites:
            console.print(
                Text.assemble(
                    ("Running evaluations in ", "bold"),
                    (suite_func.__name__, "bold blue"),
                )
            )
            for model in models_list:
                task = asyncio.create_task(
                    suite_func(
                        config=config,
                        base_url=base_url,
                        model=model,
                        max_concurrency=max_concurrent,
                    )
                )
                tasks.append(task)

        # Track progress and results as suite functions complete
        with tqdm(total=len(tasks), desc="Evaluations Progress") as pbar:
            results = []
            for f in asyncio.as_completed(tasks):
                results.append(await f)
                pbar.update(1)

        # TODO error handling on each eval
        all_evaluations.extend(results)
        display_eval_results(all_evaluations, show_details=show_details)

    asyncio.run(run_evaluations())


@cli.command(
    help="Start Arcade Worker serving tools installed in the current python environment",
    rich_help_panel="Launch",
)
def serve(
    host: str = typer.Option(
        "127.0.0.1",
        help="Host for the app, from settings by default.",
        show_default=True,
    ),
    port: int = typer.Option(
        "8002", "-p", "--port", help="Port for the app, defaults to ", show_default=True
    ),
    disable_auth: bool = typer.Option(
        False,
        "--no-auth",
        help="Disable authentication for the worker. Not recommended for production.",
        show_default=True,
    ),
    otel_enable: bool = typer.Option(
        False, "--otel-enable", help="Send logs to OpenTelemetry", show_default=True
    ),
    debug: bool = typer.Option(False, "--debug", "-d", help="Show debug information"),
) -> None:
    """
    Start a local Arcade Worker server.
    """
    workerup(host, port, disable_auth, otel_enable, debug)


@cli.command(help="Launch Arcade Worker and Engine locally", rich_help_panel="Launch")
def dev(
    host: str = typer.Option("127.0.0.1", help="Host for the worker server.", show_default=True),
    port: int = typer.Option(
        8002, "-p", "--port", help="Port for the worker server.", show_default=True
    ),
    engine_config: str = typer.Option(
        None, "-c", "--config", help="Path to the engine configuration file."
    ),
    env_file: str = typer.Option(
        None, "-e", "--env-file", help="Path to the environment variables file."
    ),
    debug: bool = typer.Option(False, "-d", "--debug", help="Show debug information"),
) -> None:
    """
    Start both the worker and engine servers.
    """
    try:
        start_servers(host, port, engine_config, engine_env=env_file, debug=debug)
    except Exception as e:
        error_message = f"❌ Failed to start servers: {escape(str(e))}"
        console.print(error_message, style="bold red")
        typer.Exit(code=1)


# TODO: deprecate this next major version
@cli.command(help="Start a local Arcade Worker server", rich_help_panel="Launch", hidden=True)
def workerup(
    host: str = typer.Option(
        "127.0.0.1",
        help="Host for the app, from settings by default.",
        show_default=True,
    ),
    port: int = typer.Option(
        "8002", "-p", "--port", help="Port for the app, defaults to ", show_default=True
    ),
    disable_auth: bool = typer.Option(
        False,
        "--no-auth",
        help="Disable authentication for the worker. Not recommended for production.",
        show_default=True,
    ),
    otel_enable: bool = typer.Option(
        False, "--otel-enable", help="Send logs to OpenTelemetry", show_default=True
    ),
    debug: bool = typer.Option(False, "--debug", "-d", help="Show debug information"),
) -> None:
    """
    Starts the worker with host, port, and reload options. Uses
    Uvicorn as ASGI worker. Parameters allow runtime configuration.
    """
    from arcade.cli.serve import serve_default_worker

    try:
        serve_default_worker(
            host, port, disable_auth=disable_auth, enable_otel=otel_enable, debug=debug
        )
    except KeyboardInterrupt:
        typer.Exit()
    except Exception as e:
        error_message = f"❌ Failed to start Arcade Worker: {escape(str(e))}"
        console.print(error_message, style="bold red")
        typer.Exit(code=1)


@cli.command(help="Deploy worker to Arcade Cloud", rich_help_panel="Deployment")
def deploy(
    deployment_file: str = typer.Option(
        "worker.toml", "--deployment-file", "-d", help="The deployment file to deploy."
    ),
    cloud_host: str = typer.Option(
        PROD_CLOUD_HOST,
        "--cloud-host",
        "-c",
        help="The Arcade Cloud host to deploy to.",
        hidden=True,
    ),
    cloud_port: int = typer.Option(
        None,
        "--cloud-port",
        "-cp",
        help="The port of the Arcade Cloud host.",
        hidden=True,
    ),
    host: str = typer.Option(
        PROD_ENGINE_HOST,
        "--host",
        "-h",
        help="The Arcade Engine host to register the worker to.",
    ),
    port: int = typer.Option(
        None,
        "--port",
        "-p",
        help="The port of the Arcade Engine host.",
    ),
    force_tls: bool = typer.Option(
        False,
        "--tls",
        help="Whether to force TLS for the connection to the Arcade Engine. If not specified, the connection will use TLS if the engine URL uses a 'https' scheme.",
    ),
    force_no_tls: bool = typer.Option(
        False,
        "--no-tls",
        help="Whether to disable TLS for the connection to the Arcade Engine.",
    ),
) -> None:
    """
    Deploy a worker to Arcade Cloud.
    """

    config = validate_and_get_config()
    engine_url = compute_base_url(force_tls, force_no_tls, host, port)
    engine_client = Arcade(api_key=config.api.key, base_url=engine_url)
    cloud_url = compute_base_url(force_tls, force_no_tls, cloud_host, cloud_port)
    cloud_client = httpx.Client(
        base_url=cloud_url, headers={"Authorization": f"Bearer {config.api.key}"}
    )

    # Fetch deployment configuration
    try:
        deployment = Deployment.from_toml(Path(deployment_file))
    except Exception as e:
        console.print(f"❌ Failed to parse deployment file: {e}", style="bold red")
        raise typer.Exit(code=1)

    with console.status(f"Deploying {len(deployment.worker)} workers"):
        for worker in deployment.worker:
            console.log(f"Deploying '{worker.config.id}...'", style="dim")
            try:
                # Attempt to deploy worker
                response = worker.request().execute(cloud_client, engine_client)
                parse_deployment_response(response)
                console.log(f"✅ Worker '{worker.config.id}' deployed successfully.", style="dim")
            except Exception as e:
                console.log(
                    f"❌ Failed to deploy worker '{worker.config.id}': {e}", style="bold red"
                )
                raise typer.Exit(code=1)


@cli.callback()
def main_callback(
    ctx: typer.Context,
    _: Optional[bool] = typer.Option(
        None,
        "-v",
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Print version and exit.",
    ),
) -> None:
    excluded_commands = {login.__name__, logout.__name__, serve.__name__, workerup.__name__}
    if ctx.invoked_subcommand in excluded_commands:
        return

    if not check_existing_login(suppress_message=True):
        console.print("Not logged in to Arcade CLI. Use ", style="bold red", end="")
        console.print("arcade login", style="bold green")
        raise typer.Exit()
