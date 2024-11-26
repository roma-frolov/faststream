from abc import abstractmethod
from collections.abc import Iterable, Sequence
from itertools import chain
from typing import TYPE_CHECKING, Any, Callable, Optional

import anyio
from aiokafka import TopicPartition
from aiokafka.errors import ConsumerStoppedError, KafkaError
from typing_extensions import override

from faststream._internal.subscriber.mixins import ConcurrentMixin, TasksMixin
from faststream._internal.subscriber.usecase import SubscriberUsecase
from faststream._internal.subscriber.utils import process_msg
from faststream._internal.types import (
    AsyncCallable,
    BrokerMiddleware,
    CustomCallable,
    MsgType,
)
from faststream._internal.utils.path import compile_path
from faststream.kafka.message import KafkaAckableMessage, KafkaMessage
from faststream.kafka.parser import AioKafkaBatchParser, AioKafkaParser
from faststream.kafka.publisher.fake import KafkaFakePublisher

if TYPE_CHECKING:
    from aiokafka import AIOKafkaConsumer, ConsumerRecord
    from aiokafka.abc import ConsumerRebalanceListener
    from fast_depends.dependencies import Dependant

    from faststream._internal.basic_types import AnyDict
    from faststream._internal.publisher.proto import BasePublisherProto
    from faststream._internal.state import BrokerState
    from faststream.message import StreamMessage
    from faststream.middlewares import AckPolicy


class LogicSubscriber(TasksMixin, SubscriberUsecase[MsgType]):
    """A class to handle logic for consuming messages from Kafka."""

    topics: Sequence[str]
    group_id: Optional[str]

    builder: Optional[Callable[..., "AIOKafkaConsumer"]]
    consumer: Optional["AIOKafkaConsumer"]

    client_id: Optional[str]
    batch: bool
    parser: AioKafkaParser

    def __init__(
        self,
        *topics: str,
        # Kafka information
        group_id: Optional[str],
        connection_args: "AnyDict",
        listener: Optional["ConsumerRebalanceListener"],
        pattern: Optional[str],
        partitions: Iterable["TopicPartition"],
        # Subscriber args
        default_parser: "AsyncCallable",
        default_decoder: "AsyncCallable",
        ack_policy: "AckPolicy",
        no_reply: bool,
        broker_dependencies: Iterable["Dependant"],
        broker_middlewares: Iterable["BrokerMiddleware[MsgType]"],
    ) -> None:
        super().__init__(
            default_parser=default_parser,
            default_decoder=default_decoder,
            # Propagated args
            ack_policy=ack_policy,
            no_reply=no_reply,
            broker_middlewares=broker_middlewares,
            broker_dependencies=broker_dependencies,
        )

        self.topics = topics
        self.partitions = partitions
        self.group_id = group_id

        self._pattern = pattern
        self.__listener = listener
        self.__connection_args = connection_args

        # Setup it later
        self.client_id = ""
        self.builder = None

        self.consumer = None

    @override
    def _setup(  # type: ignore[override]
        self,
        *,
        client_id: Optional[str],
        builder: Callable[..., "AIOKafkaConsumer"],
        # basic args
        extra_context: "AnyDict",
        # broker options
        broker_parser: Optional["CustomCallable"],
        broker_decoder: Optional["CustomCallable"],
        # dependant args
        state: "BrokerState",
    ) -> None:
        self.client_id = client_id
        self.builder = builder

        super()._setup(
            extra_context=extra_context,
            broker_parser=broker_parser,
            broker_decoder=broker_decoder,
            state=state,
        )

    async def start(self) -> None:
        """Start the consumer."""
        assert self.builder, "You should setup subscriber at first."  # nosec B101

        self.consumer = consumer = self.builder(
            group_id=self.group_id,
            client_id=self.client_id,
            **self.__connection_args,
        )

        self.parser._setup(consumer)

        if self.topics or self._pattern:
            consumer.subscribe(
                topics=self.topics,
                pattern=self._pattern,
                listener=self.__listener,
            )

        elif self.partitions:
            consumer.assign(partitions=self.partitions)

        await consumer.start()
        await super().start()

        if self.calls:
            self.add_task(self._consume())

    async def close(self) -> None:
        await super().close()

        if self.consumer is not None:
            await self.consumer.stop()
            self.consumer = None

    @override
    async def get_one(
        self,
        *,
        timeout: float = 5.0,
    ) -> "Optional[StreamMessage[MsgType]]":
        assert self.consumer, "You should start subscriber at first."  # nosec B101
        assert (  # nosec B101
            not self.calls
        ), "You can't use `get_one` method if subscriber has registered handlers."

        raw_messages = await self.consumer.getmany(
            timeout_ms=timeout * 1000,
            max_records=1,
        )

        if not raw_messages:
            return None

        ((raw_message,),) = raw_messages.values()

        context = self._state.get().di_state.context

        return await process_msg(
            msg=raw_message,
            middlewares=(
                m(raw_message, context=context) for m in self._broker_middlewares
            ),
            parser=self._parser,
            decoder=self._decoder,
        )

    def _make_response_publisher(
        self,
        message: "StreamMessage[Any]",
    ) -> Sequence["BasePublisherProto"]:
        return (
            KafkaFakePublisher(
                self._state.get().producer,
                topic=message.reply_to,
            ),
        )

    @abstractmethod
    async def get_msg(self) -> MsgType:
        raise NotImplementedError

    async def _consume(self) -> None:
        assert self.consumer, "You should start subscriber at first."  # nosec B101

        connected = True
        while self.running:
            try:
                msg = await self.get_msg()

            # pragma: no cover
            except KafkaError:  # noqa: PERF203
                if connected:
                    connected = False
                await anyio.sleep(5)

            except ConsumerStoppedError:
                return

            else:
                if not connected:  # pragma: no cover
                    connected = True

                if msg:
                    await self.consume_one(msg)

    async def consume_one(self, msg: MsgType) -> None:
        await self.consume(msg)

    @property
    def topic_names(self) -> list[str]:
        if self._pattern:
            return [self._pattern]
        if self.topics:
            return list(self.topics)
        return [f"{p.topic}-{p.partition}" for p in self.partitions]

    @staticmethod
    def build_log_context(
        message: Optional["StreamMessage[Any]"],
        topic: str,
        group_id: Optional[str] = None,
    ) -> dict[str, str]:
        return {
            "topic": topic,
            "group_id": group_id or "",
            "message_id": getattr(message, "message_id", ""),
        }

    def add_prefix(self, prefix: str) -> None:
        self.topics = tuple(f"{prefix}{t}" for t in self.topics)

        self.partitions = [
            TopicPartition(
                topic=f"{prefix}{p.topic}",
                partition=p.partition,
            )
            for p in self.partitions
        ]


class DefaultSubscriber(LogicSubscriber["ConsumerRecord"]):
    def __init__(
        self,
        *topics: str,
        # Kafka information
        group_id: Optional[str],
        listener: Optional["ConsumerRebalanceListener"],
        pattern: Optional[str],
        connection_args: "AnyDict",
        partitions: Iterable["TopicPartition"],
        # Subscriber args
        ack_policy: "AckPolicy",
        no_reply: bool,
        broker_dependencies: Iterable["Dependant"],
        broker_middlewares: Iterable["BrokerMiddleware[ConsumerRecord]"],
    ) -> None:
        if pattern:
            reg, pattern = compile_path(
                pattern,
                replace_symbol=".*",
                patch_regex=lambda x: x.replace(r"\*", ".*"),
            )

        else:
            reg = None

        self.parser = AioKafkaParser(
            msg_class=KafkaMessage
            if ack_policy is ack_policy.ACK_FIRST
            else KafkaAckableMessage,
            regex=reg,
        )

        super().__init__(
            *topics,
            group_id=group_id,
            listener=listener,
            pattern=pattern,
            connection_args=connection_args,
            partitions=partitions,
            # subscriber args
            default_parser=self.parser.parse_message,
            default_decoder=self.parser.decode_message,
            # Propagated args
            ack_policy=ack_policy,
            no_reply=no_reply,
            broker_middlewares=broker_middlewares,
            broker_dependencies=broker_dependencies,
        )

    async def get_msg(self) -> "ConsumerRecord":
        assert self.consumer, "You should setup subscriber at first."  # nosec B101
        return await self.consumer.getone()

    def get_log_context(
        self,
        message: Optional["StreamMessage[ConsumerRecord]"],
    ) -> dict[str, str]:
        if message is None:
            topic = ",".join(self.topic_names)
        else:
            topic = message.raw_message.topic

        return self.build_log_context(
            message=message,
            topic=topic,
            group_id=self.group_id,
        )


class ConcurrentDefaultSubscriber(ConcurrentMixin, DefaultSubscriber):
    def __init__(
        self,
        *topics: str,
        # Kafka information
        group_id: Optional[str],
        listener: Optional["ConsumerRebalanceListener"],
        pattern: Optional[str],
        connection_args: "AnyDict",
        partitions: Iterable["TopicPartition"],
        # Subscriber args
        max_workers: int,
        ack_policy: "AckPolicy",
        no_reply: bool,
        broker_dependencies: Iterable["Dependant"],
        broker_middlewares: Iterable["BrokerMiddleware[ConsumerRecord]"],
    ) -> None:
        super().__init__(
            *topics,
            group_id=group_id,
            listener=listener,
            pattern=pattern,
            connection_args=connection_args,
            partitions=partitions,
            max_workers=max_workers,
            # Propagated args
            ack_policy=ack_policy,
            no_reply=no_reply,
            broker_middlewares=broker_middlewares,
            broker_dependencies=broker_dependencies,
        )

    async def start(self) -> None:
        await super().start()
        self.start_consume_task()

    async def consume_one(self, msg: "ConsumerRecord") -> None:
        await self._put_msg(msg)


class BatchSubscriber(LogicSubscriber[tuple["ConsumerRecord", ...]]):
    def __init__(
        self,
        *topics: str,
        batch_timeout_ms: int,
        max_records: Optional[int],
        # Kafka information
        group_id: Optional[str],
        listener: Optional["ConsumerRebalanceListener"],
        pattern: Optional[str],
        connection_args: "AnyDict",
        partitions: Iterable["TopicPartition"],
        # Subscriber args
        ack_policy: "AckPolicy",
        no_reply: bool,
        broker_dependencies: Iterable["Dependant"],
        broker_middlewares: Iterable[
            "BrokerMiddleware[Sequence[tuple[ConsumerRecord, ...]]]"
        ],
    ) -> None:
        self.batch_timeout_ms = batch_timeout_ms
        self.max_records = max_records

        if pattern:
            reg, pattern = compile_path(
                pattern,
                replace_symbol=".*",
                patch_regex=lambda x: x.replace(r"\*", ".*"),
            )

        else:
            reg = None

        self.parser = AioKafkaBatchParser(
            msg_class=KafkaMessage
            if ack_policy is ack_policy.ACK_FIRST
            else KafkaAckableMessage,
            regex=reg,
        )

        super().__init__(
            *topics,
            group_id=group_id,
            listener=listener,
            pattern=pattern,
            connection_args=connection_args,
            partitions=partitions,
            # subscriber args
            default_parser=self.parser.parse_message,
            default_decoder=self.parser.decode_message,
            # Propagated args
            ack_policy=ack_policy,
            no_reply=no_reply,
            broker_middlewares=broker_middlewares,
            broker_dependencies=broker_dependencies,
        )

    async def get_msg(self) -> tuple["ConsumerRecord", ...]:
        assert self.consumer, "You should setup subscriber at first."  # nosec B101

        messages = await self.consumer.getmany(
            timeout_ms=self.batch_timeout_ms,
            max_records=self.max_records,
        )

        if not messages:  # pragma: no cover
            await anyio.sleep(self.batch_timeout_ms / 1000)
            return ()

        return tuple(chain(*messages.values()))

    def get_log_context(
        self,
        message: Optional["StreamMessage[tuple[ConsumerRecord, ...]]"],
    ) -> dict[str, str]:
        if message is None:
            topic = ",".join(self.topic_names)
        else:
            topic = message.raw_message[0].topic

        return self.build_log_context(
            message=message,
            topic=topic,
            group_id=self.group_id,
        )
