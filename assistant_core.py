import asyncio
import collections
import io
import math
import os
import random
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import wave
import webbrowser

from array import array
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus

import requests
from dotenv import load_dotenv


# =====================================================
# CONFIGURATION
# =====================================================

load_dotenv(Path(__file__).with_name(".env"))

def env(name, default=""):
    return (os.getenv(name) or default).strip()


ASSISTANT_NAME = env("ASSISTANT_NAME", "EDITH")
USER_NAME = env("USER_NAME", "Madhu")
CREATOR_NAME = "Madhu"

GROQ_API_KEY = env("GROQ_API_KEY")
GROQ_MODEL = env("GROQ_MODEL", "openai/gpt-oss-20b")
STT_MODEL = env("STT_MODEL", "whisper-large-v3-turbo")

DEFAULT_LANGUAGE = env("DEFAULT_LANGUAGE", "te")
AUTO_LANGUAGE_DETECTION = env(
    "AUTO_LANGUAGE_DETECTION", "true"
).lower() == "true"

VOICE_EN = env("VOICE_EN", "en-IN-NeerjaNeural")
VOICE_HI = env("VOICE_HI", "hi-IN-SwaraNeural")
VOICE_TE = env("VOICE_TE", "te-IN-ShrutiNeural")

TTS_RATE = env("TTS_RATE", "+0%")
TTS_PITCH = env("TTS_PITCH", "+0Hz")

MIC_THRESHOLD = float(env("MIC_THRESHOLD", "500"))

GROQ_BASE = "https://api.groq.com/openai/v1"

RATE = 16000
CHUNK = 1024

SILENCE_CHUNKS = int(0.9 * RATE / CHUNK)
MAX_CHUNKS = int(25 * RATE / CHUNK)
MIN_CHUNKS = int(0.5 * RATE / CHUNK)

MEMORY_DB = Path(__file__).with_name("edith_memory.sqlite3")


# =====================================================
# ERRORS
# =====================================================

class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


class RateLimit(Exception):
    pass


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)

    if h:
        return f"{h}h {m}m"

    if m:
        return f"{m}m {s}s"

    return f"{s}s"


## =====================================================
# MEMORY DATABASE
# =====================================================

def init_memory():

    with sqlite3.connect(MEMORY_DB) as db:

        # Create memories table
        db.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory TEXT,
                created_at TEXT
            )
        """)

        # Create reminders table
        db.execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message TEXT,
                remind_at TEXT,
                completed INTEGER DEFAULT 0
            )
        """)

        # Check existing memory columns
        memory_columns = {
            row[1] for row in db.execute(
                "PRAGMA table_info(memories)"
            )
        }

        # Check existing reminder columns
        reminder_columns = {
            row[1] for row in db.execute(
                "PRAGMA table_info(reminders)"
            )
        }

        # Repair missing memory columns
        if "memory" not in memory_columns:
            db.execute(
                "ALTER TABLE memories ADD COLUMN memory TEXT"
            )

        if "created_at" not in memory_columns:
            db.execute(
                "ALTER TABLE memories ADD COLUMN created_at TEXT"
            )

        # Repair missing reminder columns
        if "message" not in reminder_columns:
            db.execute(
                "ALTER TABLE reminders ADD COLUMN message TEXT"
            )

        if "remind_at" not in reminder_columns:
            db.execute(
                "ALTER TABLE reminders ADD COLUMN remind_at TEXT"
            )

        if "completed" not in reminder_columns:
            db.execute(
                "ALTER TABLE reminders ADD COLUMN completed INTEGER DEFAULT 0"
            )

        db.commit()


def remember(text):

    text = text.strip()

    if not text:
        return

    with sqlite3.connect(MEMORY_DB) as db:

        db.execute("""
            INSERT INTO memories(memory, created_at)
            VALUES (?, ?)
        """, (text, datetime.now().isoformat()))

        db.commit()


def recall_memory():

    with sqlite3.connect(MEMORY_DB) as db:

        rows = db.execute("""
            SELECT memory
            FROM memories
            WHERE memory IS NOT NULL
            AND memory != ''
            ORDER BY id DESC
            LIMIT 15
        """).fetchall()

    return [row[0] for row in rows]


def add_reminder(message, minutes):

    remind_at = datetime.now() + timedelta(minutes=minutes)

    with sqlite3.connect(MEMORY_DB) as db:

        db.execute("""
            INSERT INTO reminders(message, remind_at)
            VALUES (?, ?)
        """, (message, remind_at.isoformat()))

        db.commit()

    return remind_at


def get_due_reminders():

    now = datetime.now().isoformat()

    with sqlite3.connect(MEMORY_DB) as db:

        rows = db.execute("""
            SELECT id, message
            FROM reminders
            WHERE remind_at IS NOT NULL
            AND remind_at != ''
            AND message IS NOT NULL
            AND completed = 0
            AND remind_at <= ?
        """, (now,)).fetchall()

        for reminder_id, message in rows:

            db.execute("""
                UPDATE reminders
                SET completed = 1
                WHERE id = ?
            """, (reminder_id,))

        db.commit()

    return rows


# =====================================================
# GROQ AI CONNECTION
# =====================================================

def groq_headers():

    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json"
    }


def groq_chat(history):

    if not GROQ_API_KEY:
        raise ApiError("Groq API key is missing in .env")

    system = f"""
You are EDITH, an intelligent personal AI assistant.

Your creator is Madhu.

The user's name is {USER_NAME}.

PERSONALITY:
- Friendly, intelligent, confident and helpful.
- Speak naturally like a human assistant.
- Be slightly humorous when appropriate.
- Keep voice replies concise.
- Never pretend to perform an action you did not perform.

LANGUAGE:
- Support Telugu, English, Hindi and other languages.
- Detect the language used by the user.
- Reply in the same language.
- For Telugu, write Telugu in Telugu script.
- For Hindi, write Hindi in Devanagari.
- Support mixed Telugu-English conversations.
- Default language is Telugu.

CAPABILITIES:
- Answer general questions.
- Explain programming concepts.
- Help with Python, Django, Java and AI.
- Help with career preparation.
- Explain weather and web-search results supplied to you.
- Use remembered facts when relevant.

Do not claim you have searched the web unless search results
were actually supplied.
Do not claim a reminder or system command succeeded unless
the application confirms it.
"""

    messages = [
        {"role": "system", "content": system}
    ] + history[-12:]

    response = requests.post(
        f"{GROQ_BASE}/chat/completions",
        headers=groq_headers(),
        json={
            "model": GROQ_MODEL,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 500
        },
        timeout=45
    )

    if response.status_code == 401:
        raise AuthError("Invalid Groq API key.")

    if response.status_code == 429:
        raise RateLimit("Groq request limit reached.")

    response.raise_for_status()

    result = response.json()

    return result["choices"][0]["message"]["content"].strip()


# =====================================================
# SPEECH RECOGNITION
# =====================================================

def transcribe(audio):

    if not GROQ_API_KEY:
        raise AuthError("GROQ_API_KEY is missing.")

    data = {
        "model": STT_MODEL,
        "response_format": "json",
        "temperature": "0",
        "language": "te"
    }

    response = requests.post(
        f"{GROQ_BASE}/audio/transcriptions",
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}"
        },
        files={
            "file": ("speech.wav", audio, "audio/wav")
        },
        data=data,
        timeout=60
    )

    if response.status_code in (401, 403):
        raise AuthError("Invalid Groq API key.")

    if response.status_code == 429:
        raise RateLimit("Speech recognition limit reached.")

    response.raise_for_status()

    return response.json().get("text", "").strip()


# =====================================================
# TELUGU / MULTILINGUAL VOICE SELECTION
# =====================================================

def voice_chain(text):

    if re.search(r"[\u0C00-\u0C7F]", text):
        return [VOICE_TE, VOICE_EN]

    if re.search(r"[\u0900-\u097F]", text):
        return [VOICE_HI, VOICE_EN]

    return [VOICE_EN, VOICE_TE]


# =====================================================
# TEXT TO SPEECH
# =====================================================

def synthesize(text, voice):

    import edge_tts

    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)

    try:

        async def generate():
            communicator = edge_tts.Communicate(
                text,
                voice,
                rate=TTS_RATE,
                pitch=TTS_PITCH
            )

            await communicator.save(path)

        asyncio.run(generate())

        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise ApiError("TTS generated an empty audio file.")

        return path

    except Exception:

        if os.path.exists(path):
            os.remove(path)

        raise


# =====================================================
# WEATHER
# =====================================================

def get_weather(city="Hyderabad"):

    try:

        url = f"https://wttr.in/{quote_plus(city)}?format=j1"

        response = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "EDITH-Assistant"}
        )

        response.raise_for_status()

        data = response.json()

        current = data["current_condition"][0]

        temperature = current["temp_C"]
        feels = current["FeelsLikeC"]
        description = current["weatherDesc"][0]["value"]
        humidity = current["humidity"]

        return (
            f"Weather in {city}: {description}. "
            f"Temperature {temperature} degrees Celsius. "
            f"Feels like {feels} degrees. "
            f"Humidity {humidity} percent."
        )

    except Exception as exc:

        return f"Weather service is unavailable: {exc}"


# =====================================================
# WEB SEARCH
# =====================================================

# =====================================================
# WEBSITE LAUNCHER AND WEB SEARCH
# =====================================================

def open_website(url):

    try:
        # Primary Windows browser launcher
        if os.name == "nt":
            os.startfile(url)
            return True

        # Other operating systems
        result = webbrowser.open(url, new=2)

        if result:
            return True

        return False

    except Exception as first_error:

        print("Primary browser error:", first_error)

        try:
            # Windows fallback
            if os.name == "nt":
                subprocess.Popen(
                    ["cmd.exe", "/c", "start", "", url],
                    shell=False
                )
                return True

            return bool(webbrowser.open(url, new=2))

        except Exception as second_error:

            print("Fallback browser error:", second_error)
            return False


def web_search(query):

    url = (
        "https://www.google.com/search?q="
        + quote_plus(query)
    )

    if open_website(url):
        return f"Opening Google search for {query}."

    return "Sorry, I could not open the browser."


# =====================================================
# APPLICATION LAUNCHER
# =====================================================

APP_COMMANDS = {

    "notepad": ["notepad.exe"],
    "calculator": ["calc.exe"],
    "paint": ["mspaint.exe"],
    "file explorer": ["explorer.exe"],
    "explorer": ["explorer.exe"],
    "command prompt": ["cmd.exe"],
    "terminal": ["cmd.exe"],

}


def open_application(name):

    name = name.lower().strip()

    if name in APP_COMMANDS:

        subprocess.Popen(APP_COMMANDS[name])

        return f"Opening {name}."

    if name in ("chrome", "google chrome"):

        try:
            subprocess.Popen(["cmd", "/c", "start", "", "chrome"])
            return "Opening Google Chrome."

        except Exception:
            return "Chrome could not be opened."

    if name in ("vs code", "visual studio code", "vscode"):

        try:
            subprocess.Popen(["cmd", "/c", "code"])
            return "Opening Visual Studio Code."

        except Exception:
            return "VS Code could not be opened. Check its PATH."

    return f"I don't have an application shortcut for {name}."


# =====================================================
# SAFE SYSTEM COMMANDS
# =====================================================

def system_command(command):

    command = command.lower().strip()

    if command in ("lock computer", "lock system"):

        if os.name == "nt":
            subprocess.Popen(
                ["rundll32.exe", "user32.dll,LockWorkStation"]
            )
            return "Locking your computer."

    if command in ("shutdown computer", "shutdown system"):

        if os.name == "nt":
            subprocess.Popen(
                ["shutdown", "/s", "/t", "30"]
            )
            return "Shutdown scheduled in 30 seconds."

    if command in ("cancel shutdown", "abort shutdown"):

        if os.name == "nt":
            subprocess.Popen(
                ["shutdown", "/a"]
            )
            return "Shutdown cancelled."

    if command in ("restart computer", "restart system"):

        if os.name == "nt":
            subprocess.Popen(
                ["shutdown", "/r", "/t", "30"]
            )
            return "Restart scheduled in 30 seconds."

    return None


# =====================================================
# AUDIO UTILITIES
# =====================================================

def rms(data):

    samples = array("h", data)

    if not samples:
        return 0

    return math.sqrt(
        sum(sample * sample for sample in samples) / len(samples)
    )


def wav_bytes(frames):

    buffer = io.BytesIO()

    with wave.open(buffer, "wb") as wav:

        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)

        wav.writeframes(b"".join(frames))

    return buffer.getvalue()


def clean_text(text):

    text = re.sub(r"[*#`~>|]", "", text)

    return re.sub(r"\s+", " ", text).strip()


# =====================================================
# EDITH ASSISTANT
# =====================================================

class Assistant:

    def __init__(self, emit):

        init_memory()

        self.emit = emit

        self.state = "offline"
        self.level = 0.0
        self.muted = False

        self._active = False
        self._thread = None

        self._history = []
        self._noise = 200

        self._reminder_thread = None

    @property
    def running(self):

        return (
            self._thread is not None
            and self._thread.is_alive()
        )

    def start(self):

        if self.running:
            return False

        if not GROQ_API_KEY:
            self.emit(
                "err",
                "Add GROQ_API_KEY to your .env file."
            )
            return False

        self._active = True
        self.muted = False

        self._history = []

        self._thread = threading.Thread(
            target=self._run,
            daemon=True
        )

        self._thread.start()

        return True

    def stop(self):

        self._active = False

        try:

            import pygame

            if pygame.mixer.get_init():
                pygame.mixer.music.stop()

        except Exception:
            pass

    # -------------------------------------------------
    # STARTUP
    # -------------------------------------------------

    def _intro(self):

        lines = [
            "నమస్కారం Madhu!",
            "నేను EDITH. మీ వ్యక్తిగత AI సహాయకురాలిని.",
            "నేను తెలుగు, English, Hindi మరియు ఇతర భాషల్లో మాట్లాడగలను.",
            "మీకు ఏం సహాయం కావాలి?"
        ]

        for line in lines:

            if not self._active:
                break

            self.emit("agent", line)

            self._speak(line)

    # -------------------------------------------------
    # MICROPHONE
    # -------------------------------------------------

    def _listen(self):

        import pyaudio

        pa = pyaudio.PyAudio()

        try:

            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK
            )

        except Exception as exc:

            pa.terminate()

            raise ApiError(f"Microphone error: {exc}")

        frames = []
        pre = collections.deque(maxlen=6)

        speaking = False
        loud = 0
        quiet = 0

        try:

            while self._active:

                data = stream.read(
                    CHUNK,
                    exception_on_overflow=False
                )

                if self.muted:
                    continue

                level = rms(data)

                self.level = min(1.0, level / 5000)

                threshold = max(
                    MIC_THRESHOLD,
                    self._noise * 2.8
                )

                if not speaking:

                    if level > threshold:
                        loud += 1
                    else:
                        loud = 0
                        self._noise = (
                            self._noise * 0.95 + level * 0.05
                        )

                    pre.append(data)

                    if loud >= 3:

                        speaking = True
                        frames = list(pre)
                        self.state = "hearing"

                else:

                    frames.append(data)

                    if level < threshold:
                        quiet += 1
                    else:
                        quiet = 0

                    if (
                        quiet >= SILENCE_CHUNKS
                        or len(frames) >= MAX_CHUNKS
                    ):
                        break

        finally:

            stream.stop_stream()
            stream.close()
            pa.terminate()

            self.level = 0

        if len(frames) < MIN_CHUNKS:
            return None

        return wav_bytes(frames)

    # -------------------------------------------------
    # VOICE OUTPUT
    # -------------------------------------------------

    def _speak(self, text):

        import pygame

        clean = clean_text(text)

        last_error = None

        for voice in voice_chain(clean):

            path = None

            try:

                path = synthesize(clean, voice)

                pygame.mixer.music.load(path)
                pygame.mixer.music.play()

                while (
                    self._active
                    and pygame.mixer.music.get_busy()
                ):
                    time.sleep(0.05)

                pygame.mixer.music.stop()

                return

            except Exception as exc:

                last_error = exc

            finally:

                try:
                    pygame.mixer.music.unload()
                except Exception:
                    pass

                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        self.emit(
            "err",
            f"Voice unavailable: {last_error}"
        )

        # ==========================================
    # VOICE COMMANDS
    # ==========================================
    # =================================================
    # VOICE COMMANDS
    # =================================================

    def execute_command(self, text):

        original = text.strip()
        command = original.lower().strip()

        if not command:
            return None

        # =============================================
        # YOUTUBE
        # =============================================

        youtube_words = [
            "youtube",
            "you tube",
            "you-tube",
            "యూట్యూబ్",
            "యూటుబ్",
            "యూట్యూబు",
            "यूट्यूब"
        ]

        if any(word in command for word in youtube_words):

            # Search YouTube when a search phrase is supplied
            search_prefixes = [
                "search for",
                "search youtube for",
                "find video",
                "వెతుకు",
                "సెర్చ్ చేయి"
            ]

            query = None

            for prefix in search_prefixes:

                if prefix in command:

                    query = command.split(
                        prefix, 1
                    )[1].strip()

                    break

            if query:

                url = (
                    "https://www.youtube.com/results?search_query="
                    + quote_plus(query)
                )

            else:

                url = "https://www.youtube.com"

            if open_website(url):

                if query:
                    return f"Searching YouTube for {query}."

                return "Opening YouTube."

            return "Sorry, I could not open YouTube."

        # =============================================
        # GOOGLE
        # =============================================

        if (
            "google" in command
            or "గూగుల్" in command
        ):

            if open_website("https://www.google.com"):
                return "Opening Google."

            return "Sorry, I could not open Google."

        # =============================================
        # OTHER WEBSITES
        # =============================================

        websites = {
            "gmail": "https://mail.google.com",
            "github": "https://github.com",
            "instagram": "https://www.instagram.com",
            "facebook": "https://www.facebook.com",
            "linkedin": "https://www.linkedin.com",
            "whatsapp": "https://web.whatsapp.com",
            "chatgpt": "https://chatgpt.com"
        }

        for name, url in websites.items():

            if name in command:

                if open_website(url):
                    return f"Opening {name}."

                return f"Sorry, I could not open {name}."

        # =============================================
        # MEMORY
        # =============================================

        remember_prefixes = [
            "remember that ",
            "remember "
        ]

        for prefix in remember_prefixes:

            if command.startswith(prefix):

                fact = original[len(prefix):].strip()

                if fact:

                    remember(fact)

                    return "Okay, I'll remember that."

        memory_commands = [
            "what do you remember",
            "show my memories",
            "recall memory"
        ]

        if command in memory_commands:

            memories = recall_memory()

            if not memories:
                return "I don't have any saved memories yet."

            return (
                "Here is what I remember. "
                + ". ".join(memories)
            )

        # =============================================
        # WEB SEARCH
        # =============================================

        if command.startswith("search for "):

            query = original[len("search for "):].strip()

            return web_search(query)

        if command.startswith("google "):

            query = original[len("google "):].strip()

            return web_search(query)

        # =============================================
        # WEATHER
        # =============================================

        if "weather" in command:

            city = "Hyderabad"

            match = re.search(
                r"weather in ([a-zA-Z ]+)",
                command
            )

            if match:
                city = match.group(1).strip()

            return get_weather(city)

        # =============================================
        # APPLICATIONS
        # =============================================

        if command.startswith("open "):

            app = original[5:].strip()

            return open_application(app)

        # =============================================
        # REMINDERS
        # =============================================

        match = re.search(
            r"remind me in (\d+) minutes? to (.+)",
            command
        )

        if match:

            minutes = int(match.group(1))
            message = match.group(2).strip()

            if minutes <= 0 or minutes > 10080:

                return (
                    "Please choose a reminder between "
                    "one minute and seven days."
                )

            remind_at = add_reminder(message, minutes)

            return (
                f"Reminder saved for "
                f"{remind_at.strftime('%I:%M %p')}."
            )

        # =============================================
        # SYSTEM COMMANDS
        # =============================================

        result = system_command(command)

        if result:
            return result

        return None

        # ======================================
        # YOUTUBE - ENGLISH, TELUGU, HINDI
        # ======================================

        youtube_words = [
            "youtube",
            "you tube",
            "you-tube",
            "యూట్యూబ్",
            "యూటుబ్",
            "యూట్యూబ్",
            "यूट्यूब"
        ]

        if any(word in command for word in youtube_words):

            # YouTube search commands
            search_words = [
                "search for",
                "search youtube for",
                "find video",
                "వెతుకు",
                "సెర్చ్ చేయి"
            ]

            query = None

            for word in search_words:

                if word in command:

                    query = command.split(word, 1)[1].strip()
                    break

            if query:

                url = (
                    "https://www.youtube.com/results?search_query="
                    + urllib.parse.quote(query)
                )

                webbrowser.open(url)

                return f"Searching YouTube for {query}."

            webbrowser.open("https://www.youtube.com")

            return "Opening YouTube."

        # ======================================
        # GOOGLE
        # ======================================

        if "google" in command or "గూగుల్" in command:

            webbrowser.open("https://www.google.com")

            return "Opening Google."

        # ======================================
        # OTHER WEBSITES
        # ======================================

        websites = {
            "gmail": "https://mail.google.com",
            "github": "https://github.com",
            "instagram": "https://www.instagram.com",
            "facebook": "https://www.facebook.com",
            "linkedin": "https://www.linkedin.com",
            "whatsapp": "https://web.whatsapp.com",
            "chatgpt": "https://chatgpt.com"
        }

        for name, url in websites.items():

            if name in command:

                webbrowser.open(url)

                return f"Opening {name}."

        # ======================================
        # MEMORY
        # ======================================

        if command.startswith("remember that "):

            fact = original[len("remember that "):].strip()

            remember(fact)

            return "Okay, I'll remember that."

        if command in (
            "what do you remember",
            "show my memories",
            "recall memory"
        ):

            memories = recall_memory()

            if not memories:

                return "I don't have any saved memories yet."

            return "Here is what I remember. " + ". ".join(memories)

        # ======================================
        # WEB SEARCH
        # ======================================

        if command.startswith("search for "):

            query = original[len("search for "):].strip()

            return web_search(query)

        if command.startswith("google "):

            query = original[len("google "):].strip()

            return web_search(query)

        # ======================================
        # WEATHER
        # ======================================

        if "weather" in command:

            city = "Hyderabad"

            match = re.search(
                r"weather in ([a-zA-Z ]+)",
                command
            )

            if match:

                city = match.group(1).strip()

            return get_weather(city)

        # ======================================
        # APPLICATIONS
        # ======================================

        if command.startswith("open "):

            app = original[5:].strip()

            return open_application(app)

        # ======================================
        # REMINDERS
        # ======================================

        match = re.search(
            r"remind me in (\d+) minutes? to (.+)",
            command
        )

        if match:

            minutes = int(match.group(1))
            message = match.group(2).strip()

            if minutes <= 0 or minutes > 10080:

                return "Please choose a reminder between one minute and seven days."

            remind_at = add_reminder(message, minutes)

            return (
                f"Reminder saved for "
                f"{remind_at.strftime('%I:%M %p')}."
            )

        # ======================================
        # SYSTEM COMMANDS
        # ======================================

        result = system_command(command)

        if result:

            return result

        return None

    # -------------------------------------------------
    # REMINDER MONITOR
    # -------------------------------------------------

    def _check_reminders(self):

        while self._active:

            try:

                for reminder_id, message in get_due_reminders():

                    self.emit(
                        "agent",
                        f"Reminder: {message}"
                    )

                    if self._active:
                        self._speak(f"Reminder. {message}")

            except Exception as exc:

                self.emit("err", str(exc))

            time.sleep(5)

    # -------------------------------------------------
    # MAIN LOOP
    # -------------------------------------------------

    def _run(self):

        try:

            import pygame

            pygame.mixer.init()

            self.state = "speaking"

            self._intro()

            self._reminder_thread = threading.Thread(
                target=self._check_reminders,
                daemon=True
            )

            self._reminder_thread.start()

            while self._active:

                self.state = "listening"

                audio = self._listen()

                if not audio:
                    continue

                self.state = "thinking"

                try:

                    heard = transcribe(audio)

                    if len(heard.strip()) < 2:
                        continue

                    self.emit("user", heard)

                    # First try local commands.
                    command_reply = self.execute_command(heard)

                    if command_reply:

                        self.emit("agent", command_reply)

                        self.state = "speaking"

                        self._speak(command_reply)

                        continue

                    # Otherwise ask AI.
                    self._history.append({
                        "role": "user",
                        "content": heard
                    })

                    memories = recall_memory()

                    if memories:

                        self._history.append({
                            "role": "assistant",
                            "content": (
                                "Relevant saved memories: "
                                + "; ".join(memories[-5:])
                            )
                        })

                    reply = groq_chat(self._history)

                    self._history.append({
                        "role": "assistant",
                        "content": reply
                    })

                    self._history = self._history[-14:]

                    self.emit("agent", reply)

                    self.state = "speaking"

                    self._speak(reply)

                except RateLimit:

                    self.emit(
                        "err",
                        "Groq rate limit reached. Please wait."
                    )

                    time.sleep(10)

                except AuthError as exc:

                    self.emit("err", str(exc))

                    self._active = False

                except Exception as exc:

                    self.emit(
                        "err",
                        f"Processing error: {str(exc)[:200]}"
                    )

                    time.sleep(2)

        except Exception as exc:

            self.emit("err", str(exc))

        finally:

            self._active = False
            self.state = "offline"
            self.level = 0

            try:

                import pygame

                if pygame.mixer.get_init():
                    pygame.mixer.music.stop()

            except Exception:
                pass

            self.emit("ended", "")