import discord
import json
import os
from datetime import datetime, time, timedelta
import pytz # For timezone handling
from discord.ext import tasks # Import tasks for background loops
import nest_asyncio
import webserver

nest_asyncio.apply()

# --- Configuration ---
CONFIG_FILE = 'config.json'
SCHEDULE_FILE = 'schedule.json'

# Load configuration
def load_config():
    """Loads bot configuration from config.json."""
    if not os.path.exists(CONFIG_FILE):
        print(f"Error: {CONFIG_FILE} not found. Please create it with your bot token and owner ID.")
        exit()
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

config = load_config()
TOKEN = config.get('DISCORD_BOT_TOKEN')
BOT_OWNER_ID = int(config.get('BOT_OWNER_ID')) # Ensure owner ID is an integer

# --- Bot Setup ---
# Define intents: necessary for the bot to receive certain events
intents = discord.Intents.default()
intents.message_content = True # Required to read message content for commands

client = discord.Client(intents=intents)

# --- Schedule and User Data Management ---
def load_data():
    """Loads schedule and user timezone data from schedule.json."""
    if not os.path.exists(SCHEDULE_FILE):
        return {"streams": [], "user_timezones": {}, "announcement_channel_id": None, "last_announced_date_utc": None}
    with open(SCHEDULE_FILE, 'r') as f:
        try:
            data = json.load(f)
            # Ensure new fields exist for backward compatibility
            if "announcement_channel_id" not in data:
                data["announcement_channel_id"] = None
            if "last_announced_date_utc" not in data:
                data["last_announced_date_utc"] = None
            return data
        except json.JSONDecodeError:
            print(f"Warning: {SCHEDULE_FILE} is empty or malformed. Initializing with empty data.")
            return {"streams": [], "user_timezones": {}, "announcement_channel_id": None, "last_announced_date_utc": None}

def save_data(data):
    """Saves schedule and user timezone data to schedule.json."""
    with open(SCHEDULE_FILE, 'w') as f:
        json.dump(data, f, indent=4)

# Load initial data
bot_data = load_data()

# --- Timezone Utility Functions ---
def get_user_timezone(user_id):
    """Retrieves a user's saved timezone or returns a default."""
    return bot_data["user_timezones"].get(str(user_id), "UTC") # Default to UTC if not set

def set_user_timezone(user_id, timezone_name):
    """Sets a user's timezone preference."""
    try:
        pytz.timezone(timezone_name) # Validate timezone name
        bot_data["user_timezones"][str(user_id)] = timezone_name
        save_data(bot_data)
        return True
    except pytz.exceptions.UnknownTimeZoneError:
        return False

def clean_old_streams():
    """Removes streams that have already passed from the schedule."""
    global bot_data # Declare global to modify the bot_data dictionary directly
    now_utc = datetime.now(pytz.utc)
    updated_streams = []
    removed_count = 0

    for stream in bot_data["streams"]:
        try:
            stream_dt_naive = datetime.strptime(stream["datetime"], "%Y-%m-%d %H:%M")
            original_tz = pytz.timezone(stream["original_timezone"])
            stream_dt_localized = original_tz.localize(stream_dt_naive)

            if stream_dt_localized > now_utc:
                updated_streams.append(stream)
            else:
                removed_count += 1
        except (ValueError, pytz.exceptions.UnknownTimeZoneError) as e:
            print(f"Warning: Skipping malformed or timezone-error stream entry during cleanup: {stream} - {e}")
            updated_streams.append(stream) # Keep malformed entries for manual inspection/correction
            continue

    if removed_count > 0:
        bot_data["streams"] = updated_streams
        save_data(bot_data)
        print(f"Cleaned up {removed_count} old stream(s) from the schedule.")
    else:
        print("No old streams to clean up.")


# --- Background Task for Schedule Announcements ---
@tasks.loop(time=time(2, 3, 0, tzinfo=pytz.utc)) # Run daily at midnight UTC
async def daily_schedule_announcer():
    """Automatically announces the upcoming stream schedule to a designated channel."""
    # First, clean up any old streams
    clean_old_streams()

    channel_id = bot_data.get("announcement_channel_id")
    if not channel_id:
        print("No announcement channel set. Skipping daily schedule announcement.")
        return

    # Get current date in UTC
    today_utc = datetime.now(pytz.utc).date()

    # Check if we've already announced today
    if bot_data["last_announced_date_utc"] == str(today_utc):
        print(f"Schedule already announced for {today_utc}. Skipping.")
        return

    channel = client.get_channel(channel_id)
    if not channel:
        print(f"Could not find announcement channel with ID: {channel_id}. It might have been deleted.")
        return

    now_utc = datetime.now(pytz.utc)

    upcoming_streams = []
    # Iterate through the already cleaned bot_data["streams"]
    for stream in bot_data["streams"]:
        try:
            stream_dt_naive = datetime.strptime(stream["datetime"], "%Y-%m-%d %H:%M")
            original_tz = pytz.timezone(stream["original_timezone"])
            stream_dt_localized = original_tz.localize(stream_dt_naive)

            # This check is technically redundant if clean_old_streams ran, but good for safety
            if stream_dt_localized > now_utc:
                # Convert to Unix timestamp for Discord's timecode
                unix_timestamp = int(stream_dt_localized.timestamp())
                upcoming_streams.append((unix_timestamp, stream["description"]))
        except ValueError:
            print(f"Skipping malformed stream entry during announcement: {stream}")
            continue
        except pytz.exceptions.UnknownTimeZoneError:
            print(f"Skipping stream with unknown original timezone during announcement: {stream}")
            continue

    if not upcoming_streams:
        await channel.send("No upcoming streams scheduled! Stay tuned for updates.")
        bot_data["last_announced_date_utc"] = str(today_utc)
        save_data(bot_data)
        return

    # Sort streams by timestamp
    upcoming_streams.sort(key=lambda x: x[0])

    response_lines = ["**📢 Daily Stream Schedule Update! 📢**"]
    response_lines.append("Here are the upcoming streams:")

    for unix_timestamp, description in upcoming_streams:
        # Using Discord's long date/time and relative time formats
        response_lines.append(
            f"• <t:{unix_timestamp}:F> (<t:{unix_timestamp}:R>) - {description}"
        )
    response_lines.append("\n*Discord automatically converts these times to your local timezone!*")

    try:
        await channel.send("\n".join(response_lines))
        bot_data["last_announced_date_utc"] = str(today_utc) # Update last announced date
        save_data(bot_data)
    except discord.Forbidden:
        print(f"Bot does not have permissions to send messages in channel {channel_id}.")
    except Exception as e:
        print(f"Error sending daily schedule announcement: {e}")

@daily_schedule_announcer.before_loop
async def before_daily_schedule_announcer():
    await client.wait_until_ready()
    print("Daily schedule announcer is ready to start.")

# --- Discord Bot Events ---
@client.event
async def on_ready():
    """Called when the bot successfully connects to Discord."""
    print(f'Logged in as {client.user} (ID: {client.user.id})')
    print('------')
    # Clean up old streams immediately on bot start
    clean_old_streams()
    if not daily_schedule_announcer.is_running():
        daily_schedule_announcer.start() # Start the background task

@client.event
async def on_message(message):
    """Processes incoming messages for commands."""
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Ignore messages that don't start with '!'
    if not message.content.startswith('!'):
        return

    command, *args = message.content[1:].split(' ') # Split command and arguments
    command = command.lower() # Case-insensitive commands

    # --- User Commands ---
    if command == 'settimezone':
        if not args:
            await message.channel.send(
                "Please provide a timezone name. "
                "Example: `!settimezone America/New_York`. "
                "You can find valid timezone names here: [https://en.wikipedia.org/wiki/List_of_tz_database_time_zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)"
            )
            return

        timezone_name = args[0]
        if set_user_timezone(message.author.id, timezone_name):
            await message.channel.send(f"Your timezone has been set to `{timezone_name}`.")
        else:
            await message.channel.send(
                f"Invalid timezone name: `{timezone_name}`. "
                "Please use a valid IANA timezone name (e.g., `America/Los_Angeles`). "
                "Refer to: [https://en.wikipedia.org/wiki/List_of_tz_database_time_zones](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)"
            )

    elif command == 'schedule':
        # Ensure the schedule is clean before displaying
        clean_old_streams()
        now_utc = datetime.now(pytz.utc) # Get current time in UTC

        upcoming_streams = []
        for i, stream in enumerate(bot_data["streams"]): # Iterate through the already cleaned list
            try:
                # Parse stream time in its original timezone
                stream_dt_naive = datetime.strptime(stream["datetime"], "%Y-%m-%d %H:%M")
                original_tz = pytz.timezone(stream["original_timezone"])
                stream_dt_localized = original_tz.localize(stream_dt_naive)

                # This check is technically redundant if clean_old_streams ran, but good for safety
                if stream_dt_localized > now_utc:
                    # Convert to Unix timestamp for Discord's timecode
                    unix_timestamp = int(stream_dt_localized.timestamp())
                    upcoming_streams.append((unix_timestamp, stream["description"]))
            except ValueError:
                print(f"Skipping malformed stream entry: {stream}")
                continue
            except pytz.exceptions.UnknownTimeZoneError:
                print(f"Skipping stream with unknown original timezone: {stream}")
                continue

        if not upcoming_streams:
            await message.channel.send("No upcoming streams scheduled! Stay tuned for updates.")
            return

        # Sort streams by date
        upcoming_streams.sort(key=lambda x: x[0])

        response_lines = ["**Upcoming Stream Schedule:**"]
        response_lines.append("*(Times automatically shown in your local timezone)*")

        for unix_timestamp, description in upcoming_streams:
            # Using Discord's long date/time and relative time formats
            response_lines.append(
                f"• <t:{unix_timestamp}:F> (<t:{unix_timestamp}:R>) - {description}"
            )

        await message.channel.send("\n".join(response_lines))

    # --- Admin Commands (Owner Only) ---
    elif message.author.id == BOT_OWNER_ID:
        if command == 'addstream':
            # Expected format: !addstream YYYY-MM-DD HH:MM OriginalTZ Description...
            if len(args) < 3:
                await message.channel.send(
                    "Usage: `!addstream YYYY-MM-DD HH:MM OriginalTZ Description`\n"
                    "Example: `!addstream 2025-07-15 19:00 America/Los_Angeles My next gaming stream`"
                )
                return

            try:
                datetime_str = f"{args[0]} {args[1]}"
                original_tz_name = args[2]
                description = " ".join(args[3:])

                # Validate datetime format
                datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")

                # Validate timezone
                pytz.timezone(original_tz_name)

                new_stream = {
                    "datetime": datetime_str,
                    "original_timezone": original_tz_name,
                    "description": description
                }
                bot_data["streams"].append(new_stream)
                save_data(bot_data)
                await message.channel.send(f"Stream added: `{datetime_str} {original_tz_name} - {description}`")

            except ValueError:
                await message.channel.send(
                    "Invalid date/time format. Please use `YYYY-MM-DD HH:MM`."
                )
            except pytz.exceptions.UnknownTimeZoneError:
                await message.channel.send(
                    f"Invalid original timezone: `{original_tz_name}`. "
                    "Please use a valid IANA timezone name."
                )
            except Exception as e:
                await message.channel.send(f"An error occurred: {e}")

        elif command == 'removestream':
            # Ensure the schedule is clean before listing for removal
            clean_old_streams()
            # Expected format: !removestream <index>
            if not args or not args[0].isdigit():
                await message.channel.send(
                    "Usage: `!removestream <index>` (Get index from `!liststreams`)"
                )
                return

            try:
                index_to_remove = int(args[0])
                if 0 <= index_to_remove < len(bot_data["streams"]):
                    removed_stream = bot_data["streams"].pop(index_to_remove)
                    save_data(bot_data)
                    await message.channel.send(
                        f"Removed stream: `{removed_stream['datetime']} {removed_stream['original_timezone']} - {removed_stream['description']}`"
                    )
                else:
                    await message.channel.send("Invalid stream index.")
            except Exception as e:
                await message.channel.send(f"An error occurred: {e}")

        elif command == 'liststreams':
            # Ensure the schedule is clean before listing
            clean_old_streams()
            if not bot_data["streams"]:
                await message.channel.send("No streams currently stored.")
                return

            response_lines = ["**All Stored Streams (for admin reference):**"]
            response_lines.append("```")
            for i, stream in enumerate(bot_data["streams"]):
                response_lines.append(
                    f"[{i}] {stream['datetime']} {stream['original_timezone']} - {stream['description']}"
                )
            response_lines.append("```")
            await message.channel.send("\n".join(response_lines))

        elif command == 'setannouncechannel':
            if not args or not args[0].isdigit():
                await message.channel.send(
                    "Usage: `!setannouncechannel <channel_id>`\n"
                    "To get a channel ID, enable Discord Developer Mode, right-click a channel, and select 'Copy ID'."
                )
                return

            channel_id = int(args[0])
            # Verify channel exists and is a text channel
            try:
                channel = client.get_channel(channel_id)
                if channel and isinstance(channel, discord.TextChannel):
                    bot_data["announcement_channel_id"] = channel_id
                    save_data(bot_data)
                    await message.channel.send(f"Automatic schedule announcements will now be sent to <#{channel_id}>.")
                else:
                    await message.channel.send(f"Could not find a valid text channel with ID `{channel_id}`.")
            except Exception as e:
                await message.channel.send(f"An error occurred while setting the channel: {e}")
    else:
        # If a command is not recognized or not allowed for the user
        await message.channel.send(f"Unknown command: `{command}`. Try `!schedule` or `!settimezone`.")

webserver.keep_alive()

# --- Run the Bot ---
if TOKEN:
    client.run(TOKEN)
else:
    print("Discord bot token not found. Please ensure 'DISCORD_BOT_TOKEN' is set in config.json.")
