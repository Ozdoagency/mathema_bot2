from enum import Enum

class DialogState(Enum):
    START = "start"
    GOT_GOAL = "got_goal"
    GOT_CLASS = "got_class"
    GOT_TOPICS = "got_topics"
    GOT_TIME = "got_time"
    COMPLETED = "completed"

class DialogTracker:
    def __init__(self):
        self.states = {}
        
    def get_state(self, chat_id: int) -> DialogState:
        return self.states.get(chat_id, DialogState.START)
        
    def update_state(self, chat_id: int, new_state: DialogState) -> None:
        self.states[chat_id] = new_state
        
    def is_completed(self, chat_id: int) -> bool:
        return self.states.get(chat_id) == DialogState.COMPLETED
