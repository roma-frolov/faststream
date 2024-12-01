from typing import TYPE_CHECKING, Optional, Union

from faststream._internal.publisher.fake import FakePublisher
from faststream.rabbit.response import RabbitPublishCommand

if TYPE_CHECKING:
    from faststream._internal.publisher.proto import ProducerProto
    from faststream.response.response import PublishCommand


class RabbitFakePublisher(FakePublisher):
    """Publisher Interface implementation to use as RPC or REPLY TO answer publisher."""

    def __init__(
        self,
        producer: "ProducerProto",
        routing_key: str,
        app_id: Optional[str],
    ) -> None:
        super().__init__(producer=producer)
        self.routing_key = routing_key
        self.app_id = str

    def patch_command(
        self, cmd: Union["PublishCommand", "RabbitPublishCommand"]
    ) -> "RabbitPublishCommand":
        real_cmd = RabbitPublishCommand.from_cmd(cmd)
        real_cmd.destination = self.routing_key
        real_cmd.app_id = self.app_id
        return real_cmd