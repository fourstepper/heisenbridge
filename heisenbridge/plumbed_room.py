import logging
import re
from typing import Optional

from heisenbridge.channel_room import ChannelRoom
from heisenbridge.matrix import MatrixError
from heisenbridge.private_room import split_long


class NetworkRoom:
    pass


class PlumbedRoom(ChannelRoom):
    need_invite = False

    def is_valid(self) -> bool:
        # we are valid as long as the appservice is in the room
        if not self.in_room(self.serv.user_id):
            return False

        return True

    @staticmethod
    async def create(network: "NetworkRoom", id: str, channel: str, key: str) -> "ChannelRoom":
        logging.debug(f"PlumbedRoom.create(network='{network.name}', id='{id}', channel='{channel}', key='{key}'")

        try:
            resp = await network.serv.api.post_room_join_alias(id)
            join_rules = await network.serv.api.get_room_state_event(resp["room_id"], "m.room.join_rules")
        except MatrixError as e:
            network.send_notice(f"Failed to join room: {str(e)}")
            return

        room = PlumbedRoom(resp["room_id"], network.user_id, network.serv, [network.serv.user_id])
        room.name = channel.lower()
        room.key = key
        room.network = network
        room.network_name = network.name
        room.need_invite = join_rules["join_rule"] != "public"

        network.serv.register_room(room)
        network.rooms[room.name] = room
        await room.save()

        network.send_notice(f"Plumbed {resp['room_id']} to {channel}, to unplumb just kick me out.")
        return room

    def send_notice(
        self,
        text: str,
        user_id: Optional[str] = None,
        formatted=None,
        fallback_html: Optional[str] = None,
        forward=True,
    ):
        if user_id is not None or forward is False:
            super().send_notice(text=text, user_id=user_id, formatted=formatted, fallback_html=fallback_html)
            return

        self.network.send_notice(
            text=f"{self.name}: {text}", user_id=user_id, formatted=formatted, fallback_html=fallback_html
        )

    # don't try to set room topic when we're plumbed, just show it
    def set_topic(self, topic: str, user_id: Optional[str] = None) -> None:
        self.send_notice(f"New topic is: '{topic}'")

    async def on_mx_message(self, event) -> None:
        if self.network is None or self.network.conn is None or not self.network.conn.connected:
            return

        sender = event["sender"]
        (name, server) = sender.split(":")

        # prevent re-sending federated messages back
        if name.startswith("@" + self.serv.puppet_prefix) and server == self.serv.server_name:
            return

        # add ZWSP to sender to avoid pinging on IRC
        sender = f"{name[:2]}\u200B{name[2:]}:{server[:1]}\u200B{server[1:]}"

        body = None
        if "body" in event["content"]:
            body = event["content"]["body"]

            # replace mentioning us with our name
            body = body.replace(self.serv.user_id, "Heisenbridge")

            # try to replace puppet matrix id mentions with displaynames
            for user_id, displayname in self.displaynames.items():
                body = body.replace(user_id, displayname)

        if event["content"]["msgtype"] == "m.emote":
            self.network.conn.action(self.name, f"{sender} {body}")
        elif event["content"]["msgtype"] == "m.image":
            self.network.conn.privmsg(
                self.name, "<{}> {}".format(sender, self.serv.mxc_to_url(event["content"]["url"]))
            )
        elif event["content"]["msgtype"] == "m.text":
            if "m.new_content" in event["content"]:
                return

            lines = body.split("\n")

            # remove reply text but preserve mention
            if "m.relates_to" in event["content"] and "m.in_reply_to" in event["content"]["m.relates_to"]:
                # pull the mention out, it's already converted to IRC nick but the regex still matches
                m = re.match(r"> <([^>]+)>", lines.pop(0))
                reply_to = m.group(1) if m else None

                # skip all quoted lines, it will skip the next empty line as well (it better be empty)
                while len(lines) > 0 and lines.pop(0).startswith(">"):
                    pass

                # convert mention to IRC convention
                if reply_to:
                    first_line = reply_to + ": " + lines.pop(0)
                    lines.insert(0, first_line)

            messages = []

            for line in lines:
                # drop all whitespace-only lines
                if re.match(r"^\s*$", line):
                    continue

                # drop all code block lines
                if re.match(r"^\s*```\s*$", line):
                    continue

                messages += split_long(
                    self.network.conn.real_nickname,
                    self.network.conn.user,
                    self.network.real_host,
                    self.name,
                    f"<{sender}> {line}",
                )

            for i, message in enumerate(messages):
                if i == 4 and len(messages) > 5:
                    self.react(event["event_id"], "\u2702")  # scissors

                    resp = await self.serv.api.post_media_upload(
                        body.encode("utf-8"), content_type="text/plain; charset=UTF-8"
                    )
                    self.network.conn.privmsg(
                        self.name,
                        f"... long message truncated: {self.serv.mxc_to_url(resp['content_uri'])} ({len(messages)} lines)",
                    )
                    self.react(event["event_id"], "\U0001f4dd")  # memo

                    return

                self.network.conn.privmsg(self.name, message)
