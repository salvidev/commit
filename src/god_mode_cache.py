"""
god_mode_cache.py — WebSocket God Mode Manager
Menjaga 1 koneksi WebSocket per Game ID di background untuk mensuplai data real-time ke semua bot.

Event Delta Tracker:
  State snapshot hanya dikirim SEKALI oleh server saat connect.
  Setelah itu server mengirim event stream per aksi agent (agent_moved, hp_changed, dll).
  _apply_event() menerapkan setiap event ke state snapshot secara in-place,
  sehingga find_sultan() / find_killer() di god_mode.py selalu baca data yang fresh.
"""

import asyncio
import json
import websockets
from typing import Dict, Any


def _log(game_id: str, msg: str):
    gid = game_id[:8] if game_id else "?"
    print(f"[GOD-WS:{gid}] {msg}", flush=True)


class GodModeCache:
    def __init__(self):
        self.game_states: Dict[str, Dict[str, Any]] = {}
        self.ws_tasks: Dict[str, asyncio.Task] = {}
        # agent_index: {game_id: {agent_id: agent_dict}} — pointer ke dict di game_states
        # dibangun sekali saat state awal diterima, dipakai oleh _apply_event()
        self._agent_index: Dict[str, Dict[str, Any]] = {}
        self._running = True

    def _build_agent_index(self, game_id: str):
        """Build {agent_id: agent_dict} index pointing into game_states[game_id]['agents'].
        Called once after initial state snapshot. Each dict in the index is a live reference
        so mutations in _apply_event() automatically reflect in game_states."""
        state = self.game_states.get(game_id)
        if not state:
            return
        self._agent_index[game_id] = {a["id"]: a for a in state.get("agents", [])}

    def _apply_event(self, game_id: str, msg: dict):
        """Apply a single WS event to the in-memory state snapshot.

        Handled event types:
          agent_moved        → update agent.regionId
          hp_changed         → update agent.hp
          ep_changed         → update agent.ep
          inventory_changed  → replace agent.inventory (items = full new inventory)
          item_picked        → append item to agent.inventory
          agent_attacked     → if targetHp == 0: mark target dead, increment attacker kills
        """
        idx = self._agent_index.get(game_id)
        if not idx:
            return

        t = msg.get("type", "")

        if t == "agent_moved":
            agent = idx.get(msg.get("agentId"))
            if agent:
                agent["regionId"] = msg.get("toRegion", agent.get("regionId"))

        elif t == "hp_changed":
            agent = idx.get(msg.get("agentId"))
            if agent:
                agent["hp"] = msg.get("currentHp", agent.get("hp", 0))

        elif t == "ep_changed":
            agent = idx.get(msg.get("agentId"))
            if agent:
                agent["ep"] = msg.get("currentEp", agent.get("ep", 0))

        elif t == "inventory_changed":
            agent = idx.get(msg.get("agentId"))
            if agent:
                items = msg.get("items")
                if isinstance(items, list):
                    # Server sends full replacement inventory
                    agent["inventory"] = items

        elif t == "item_picked":
            agent = idx.get(msg.get("agentId"))
            if agent:
                item = msg.get("item")
                if item and isinstance(item, dict):
                    inv = agent.setdefault("inventory", [])
                    # Avoid duplicate if inventory_changed arrives alongside
                    if not any(i.get("id") == item.get("id") for i in inv):
                        inv.append(item)

        elif t == "agent_attacked":
            target_hp = msg.get("targetHp", 1)
            if target_hp == 0:
                target = idx.get(msg.get("targetId"))
                if target:
                    target["isAlive"] = False
                    target["hp"] = 0
                attacker = idx.get(msg.get("attackerId"))
                if attacker:
                    attacker["kills"] = attacker.get("kills", 0) + 1

    async def ensure_listening(self, game_id: str, api_client):
        """Pastikan ada koneksi WebSocket yang berjalan untuk game_id ini."""
        if not self._running:
            return
        if game_id in self.ws_tasks and not self.ws_tasks[game_id].done():
            return
        task = asyncio.create_task(self._ws_loop(game_id, api_client))
        self.ws_tasks[game_id] = task

    async def _ws_loop(self, game_id: str, api_client):
        headers = {
            "Origin": "https://moltyroyale.com",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0",
        }

        fail_count = 0

        while self._running:
            try:
                # Fetch WS endpoint URL
                resp = await api_client.get_ws_endpoint(game_id)

                if not resp or not resp.get("success"):
                    fail_count += 1
                    err = resp.get("error", {}) if resp else {}
                    _log(
                        game_id,
                        f"Endpoint FAILED [{fail_count}/10]: {err.get('code', '?')} — {err.get('message', 'no response')}",
                    )
                    if fail_count > 10:
                        _log(game_id, "Too many failures. Giving up.")
                        break
                    await asyncio.sleep(5)
                    continue

                fail_count = 0
                data_obj = resp.get("data", {})
                ws_url = (
                    data_obj.get("wsUrl")
                    or data_obj.get("url")
                    or data_obj.get("wsEndpoint")
                    or data_obj.get("endpoint")
                )
                if not ws_url:
                    _log(
                        game_id,
                        f"No URL in endpoint response. Keys: {list(data_obj.keys())}",
                    )
                    fail_count += 1
                    await asyncio.sleep(5)
                    continue

                # Connect WebSocket (spoofed as browser)
                async with websockets.connect(
                    ws_url,
                    extra_headers=headers,
                    max_size=10 * 1024 * 1024,
                    ping_interval=30,
                    ping_timeout=15,
                ) as ws:
                    msg_count = 0

                    while self._running:
                        msg = await ws.recv()
                        msg_count += 1
                        try:
                            data = json.loads(msg)
                        except json.JSONDecodeError:
                            continue

                        if "state" in data:
                            state_data = data["state"]
                            self.game_states[game_id] = state_data

                            # Build agent index (live pointers into state_data["agents"])
                            self._build_agent_index(game_id)

                            # Log only on first state received per connection
                            if msg_count == 1:
                                agents = len(state_data.get("agents", []))
                                regions = len(state_data.get("regions", []))
                                _log(
                                    game_id,
                                    f"CONNECTED — agents={agents} regions={regions} size={len(msg)}B",
                                )

                            # Stop when game finishes
                            if state_data.get("room", {}).get("status") == "finished":
                                _log(game_id, "Game finished — stopping listener.")
                                return

                        else:
                            # Event message — apply delta to cached state
                            self._apply_event(game_id, data)

            except asyncio.CancelledError:
                break
            except websockets.exceptions.ConnectionClosed as e:
                _log(game_id, f"Connection closed: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)
            except Exception as e:
                _log(game_id, f"Error ({type(e).__name__}): {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

        # Cleanup on exit
        self.ws_tasks.pop(game_id, None)
        self.game_states.pop(game_id, None)
        self._agent_index.pop(game_id, None)

    def get_state(self, game_id: str) -> dict | None:
        """Mengambil state terbaru dari cache."""
        return self.game_states.get(game_id)

    async def close(self):
        self._running = False
        for task in self.ws_tasks.values():
            if not task.done():
                task.cancel()
        self.game_states.clear()
        self._agent_index.clear()
