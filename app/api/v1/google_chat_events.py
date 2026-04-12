"""Google Chat Events API endpoint."""
import logging
from fastapi import APIRouter, Request, BackgroundTasks, HTTPException, Depends
from fastapi.responses import JSONResponse

from app.services.message_processor_v2 import MessageProcessorV2
from app.services.platforms.google_chat_connector import GoogleChatConnector
from app.core.dependencies import get_message_processor_v2, get_firestore_service
from app.services.firestore_service import FirestoreService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/google-chat", tags=["google-chat"])


async def handle_google_chat_event(
    request: Request,
    background_tasks: BackgroundTasks,
    agent_id: str,
    message_processor: MessageProcessorV2,
    firestore: FirestoreService,
):
    """
    Common handler for Google Chat events for a specific agent.

    Args:
        request: FastAPI request object
        background_tasks: FastAPI background tasks
        agent_id: Agent ID from Firestore agents collection
        message_processor: Message processor service (v2)
        firestore: Firestore service

    Returns:
        JSON response acknowledging receipt
    """
    # Parse JSON
    data = await request.json()

    # Google Chat events have structure: {chat: {messagePayload: {message: ...}}}
    # Check if this is a message event
    chat_data = data.get("chat", {})
    message_payload = chat_data.get("messagePayload")

    if not message_payload:
        # Not a message event - could be ADDED_TO_SPACE, REMOVED_FROM_SPACE, etc.
        logger.info(f"Received non-message Google Chat event: {list(data.keys())}")
        return JSONResponse(content={})

    # Handle message event
    try:
        message = message_payload.get("message", {})
        space = message.get("space", {})
        space_name = space.get("name")

        # Ignore bot messages to prevent loops
        sender = message.get("sender", {})
        sender_type = sender.get("type")
        if sender_type == "BOT":
            logger.debug("Ignoring bot message to prevent loops")
            # IMPORTANT: Must return {} not {"status": "ok"} - Google Chat only recognizes
            # {"text": "..."} or {} as valid webhook responses. Any other format causes
            # "Not Responding" errors. See TROUBLESHOOTING.md for details.
            return JSONResponse(content={})

        logger.info(
            f"Received message event for agent {agent_id} from space: {space_name}, "
            f"sender: {sender.get('name')}, "
            f"text: {message.get('text')}"
        )

        # Get the specific agent
        agents = await firestore.list_agents()
        agent = None
        for candidate_agent in agents:
            if candidate_agent.id == agent_id:
                agent = candidate_agent
                break

        if not agent:
            logger.error(f"Agent {agent_id} not found")
            return JSONResponse(content={})

        # Get Google Chat config
        google_chat_config = agent.get_google_chat_config()
        if not google_chat_config or not google_chat_config.enabled:
            logger.error(f"Agent {agent_id} does not have Google Chat enabled")
            return JSONResponse(content={})

        # Create Google Chat connector with agent's secret reference
        if not google_chat_config.google_chat_service_account_secret:
            logger.error(f"Agent {agent.id} has no Google Chat service account secret")
            return JSONResponse(content={})

        connector = GoogleChatConnector(
            service_account_secret_name=google_chat_config.google_chat_service_account_secret,
            project_id=google_chat_config.google_chat_project_id  # None for backward compatibility
        )

        # Parse Google Chat event into platform event
        platform_event = connector.parse_event(data)

        # Process event in background
        background_tasks.add_task(
            message_processor.process_platform_event,
            platform_event,
            connector,
            agent.id
        )

        # Return a synchronous response to prevent "not responding" message
        # The background task will send the actual agent response via the Chat API
        return JSONResponse(content={
            "text": "Processing your message..."
        })

    except Exception as e:
        logger.error(f"Error processing Google Chat event for agent {agent_id}: {e}", exc_info=True)
        # Still return 200 to acknowledge receipt
        return JSONResponse(content={})


@router.post("/events")
async def google_chat_events(
    request: Request,
    background_tasks: BackgroundTasks,
    message_processor: MessageProcessorV2 = Depends(get_message_processor_v2),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    """
    Google Chat Events API endpoint.

    Handles message events from Google Chat. For MVP, uses the first enabled
    Google Chat agent. In production, should match by bot name or space.

    Returns 200 immediately. Processes events in background.
    """
    # For MVP: Find first enabled Google Chat agent
    agents = await firestore.list_agents()
    agent_id = None

    for agent in agents:
        config = agent.get_google_chat_config()
        if config and config.enabled:
            agent_id = agent.id
            break

    if not agent_id:
        logger.error("No enabled Google Chat agent found")
        return JSONResponse(content={})

    return await handle_google_chat_event(
        request=request,
        background_tasks=background_tasks,
        agent_id=agent_id,
        message_processor=message_processor,
        firestore=firestore,
    )
