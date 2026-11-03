"""
Minimal stand-ins for the python-telegram-bot objects the handlers touch.
Only the attributes the code actually reads exist — a missing attribute is
a test failure worth seeing, not something to paper over.
"""

import asyncio


class FakeMessage:
    def __init__(self, message_id: int, chat_id: int, text: str = ""):
        self.message_id = message_id
        self.chat_id = chat_id
        self.text = text
        self.reply_markup = None


class FakeBot:
    """Records every outgoing call. delete_delay simulates network latency on
    delete_message, which is how the engine race tests hold the answer lock."""

    def __init__(self, delete_delay: float = 0.0):
        self._next_id = 100
        self.sent: list[str] = []
        self.markups: list = []   # reply_markup of each sent message (or None)
        self.deleted: list[int] = []
        self.edited: list[str] = []
        self.delete_delay = delete_delay

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.sent.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        msg = FakeMessage(self._next_id, chat_id, text)
        msg.reply_markup = kwargs.get("reply_markup")
        return msg

    async def delete_message(self, chat_id, message_id):
        await asyncio.sleep(self.delete_delay)
        self.deleted.append(message_id)

    async def edit_message_text(self, text=None, **kwargs):
        self.edited.append(text or "")

    async def send_document(self, *a, **kw):
        return None


class FakeJob:
    def __init__(self, callback, name, data, chat_id):
        self.callback, self.name, self.data, self.chat_id = callback, name, data, chat_id
        self.removed = False

    def schedule_removal(self):
        self.removed = True


class FakeJobQueue:
    def __init__(self):
        self.jobs: list[FakeJob] = []

    def run_once(self, callback, when, chat_id=None, user_id=None, data=None, name=None):
        job = FakeJob(callback, name, data, chat_id)
        self.jobs.append(job)
        return job

    def run_repeating(self, callback, interval, first=None, name=None, **kw):
        job = FakeJob(callback, name, {"interval": interval, "first": first}, None)
        self.jobs.append(job)
        return job

    def get_jobs_by_name(self, name):
        return [j for j in self.jobs if j.name == name and not j.removed]

    def live(self, prefix: str):
        return [j for j in self.jobs if j.name and j.name.startswith(prefix) and not j.removed]


class FakeApplication:
    def __init__(self, user_id: int):
        self.user_data = {user_id: {"state": {}}}
        self.job_queue = FakeJobQueue()


class FakeContext:
    """Looks enough like ContextTypes.DEFAULT_TYPE for handlers and jobs."""

    def __init__(self, user_id: int, bot: FakeBot | None = None):
        self.application = FakeApplication(user_id)
        self.bot = bot or FakeBot()
        self.job_queue = self.application.job_queue
        self.user_data = self.application.user_data[user_id]
        self.job = None
        self.error = None
        self.args: list[str] = []

    @property
    def state(self) -> dict:
        return self.user_data["state"]

    @state.setter
    def state(self, value: dict) -> None:
        self.user_data["state"] = value

    def timeout_job(self, user_id: int, question_index: int) -> "FakeContext":
        """Arm self.job the way the per-question timeout callback expects."""
        from bot.quiz_engine import _question_timer_name
        self.job = FakeJob(
            None, _question_timer_name(user_id),
            {"user_id": user_id, "question_index": question_index}, user_id,
        )
        return self


class FakeUser:
    def __init__(self, user_id: int, first_name: str = "Tester"):
        self.id = user_id
        self.first_name = first_name
        self.last_name = None
        self.username = "tester"
        self.language_code = "en"


class FakeQuery:
    def __init__(self, user_id: int, message_id: int = 77):
        self.from_user = FakeUser(user_id)
        self.message = FakeMessage(message_id, user_id)
        self.edits: list[str] = []
        self.data = ""

    async def edit_message_text(self, text=None, *a, **kw):
        self.edits.append(text or "")

    async def edit_message_reply_markup(self, reply_markup=None, **kw):
        self.message.reply_markup = reply_markup

    async def answer(self, *a, **kw):
        return None
