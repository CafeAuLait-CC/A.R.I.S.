from collections import defaultdict
from typing import Callable, Dict, List, Any

class EventBus:
    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Callable[[Any], None]):
        self._subscribers[event_type].append(handler)

    def publish(self, event_type: str, payload: Any):
        for h in self._subscribers.get(event_type, []):
            h(payload)

event_bus = EventBus()
