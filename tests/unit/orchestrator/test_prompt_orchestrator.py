# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import tempfile
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from unit.mocks import MockPromptTarget

from pyrit.models import PromptRequestPiece, PromptRequestResponse, Score
from pyrit.orchestrator import PromptSendingOrchestrator
from pyrit.prompt_converter import Base64Converter, StringJoinConverter
from pyrit.prompt_normalizer import NormalizerRequest, NormalizerRequestPiece
from pyrit.score import SubStringScorer


@pytest.fixture(scope="function")
def mock_target(patch_central_database) -> MockPromptTarget:
    return MockPromptTarget()


@patch(
    "pyrit.common.default_values.get_non_required_value",
    return_value='{"op_name": "dummy_op"}',
)
def test_init_orchestrator_global_memory_labels(get_non_required_value, mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)
    assert orchestrator._global_memory_labels == {"op_name": "dummy_op"}


@pytest.mark.asyncio
async def test_send_prompt_no_converter(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)

    await orchestrator.send_prompts_async(prompt_list=["Hello"])
    assert mock_target.prompt_sent == ["Hello"]


@pytest.mark.asyncio
async def test_send_prompts_async_no_converter(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)

    await orchestrator.send_prompts_async(prompt_list=["Hello"])
    assert mock_target.prompt_sent == ["Hello"]


@pytest.mark.asyncio
async def test_send_multiple_prompts_no_converter(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)

    await orchestrator.send_prompts_async(prompt_list=["Hello", "my", "name"])
    assert mock_target.prompt_sent == ["Hello", "my", "name"]


@pytest.mark.asyncio
async def test_send_prompts_b64_converter(mock_target: MockPromptTarget):
    converter = Base64Converter()
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target, prompt_converters=[converter])

    await orchestrator.send_prompts_async(prompt_list=["Hello"])
    assert mock_target.prompt_sent == ["SGVsbG8="]


@pytest.mark.asyncio
async def test_send_prompts_multiple_converters(mock_target: MockPromptTarget):
    b64_converter = Base64Converter()
    join_converter = StringJoinConverter(join_value="_")

    # This should base64 encode the prompt and then join the characters with an underscore
    converters = [b64_converter, join_converter]

    orchestrator = PromptSendingOrchestrator(objective_target=mock_target, prompt_converters=converters)

    await orchestrator.send_prompts_async(prompt_list=["Hello"])
    assert mock_target.prompt_sent == ["S_G_V_s_b_G_8_="]


@pytest.mark.asyncio
async def test_send_normalizer_requests_async(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)
    orchestrator._prompt_normalizer = AsyncMock()
    orchestrator._prompt_normalizer.send_prompt_batch_to_target_async = AsyncMock(return_value=None)

    with tempfile.NamedTemporaryFile(suffix=".png") as f:

        f.write(b"test")
        f.flush()
        req = NormalizerRequestPiece(
            request_converters=[Base64Converter()],
            prompt_data_type="image_path",
            prompt_value=f.name,
        )

        await orchestrator.send_normalizer_requests_async(prompt_request_list=[NormalizerRequest(request_pieces=[req])])
        assert orchestrator._prompt_normalizer.send_prompt_batch_to_target_async.called


@pytest.mark.asyncio
@pytest.mark.parametrize("num_conversations", [1, 10, 20])
async def test_send_prompts_and_score_async(mock_target: MockPromptTarget, num_conversations: int):
    # Set up mocks and return values
    scorer = SubStringScorer(
        substring="test",
        category="test",
    )

    scorer.score_async = AsyncMock()  # type: ignore

    orchestrator = PromptSendingOrchestrator(objective_target=mock_target, scorers=[scorer])
    orchestrator._prompt_normalizer = AsyncMock()

    request_pieces = []
    orchestrator_id = orchestrator.get_identifier()

    for n in range(num_conversations):
        conversation_id = str(uuid.uuid4())
        request_pieces.extend(
            [
                PromptRequestPiece(
                    role="user",
                    original_value=f"request_{n}",
                    conversation_id=conversation_id,
                    orchestrator_identifier=orchestrator_id,
                ),
                PromptRequestPiece(
                    role="assistant",
                    original_value=f"response_{n}",
                    conversation_id=conversation_id,
                    orchestrator_identifier=orchestrator_id,
                ),
            ]
        )

    orchestrator._prompt_normalizer.send_prompt_batch_to_target_async = AsyncMock(
        return_value=[piece.to_prompt_request_response() for piece in request_pieces]
    )
    func_str = "get_prompt_request_pieces"
    with patch.object(orchestrator._memory, func_str, return_value=request_pieces):  # type: ignore
        await orchestrator.send_prompts_async(
            prompt_list=[piece.original_value for piece in request_pieces if piece.role == "user"]
        )
        assert orchestrator._prompt_normalizer.send_prompt_batch_to_target_async.called
        assert scorer.score_async.call_count == num_conversations

    # Check that sending another prompt request scores the appropriate pieces
    response2 = PromptRequestPiece(
        role="assistant",
        original_value="test response to score 2",
        orchestrator_identifier=orchestrator.get_identifier(),
    )

    request_pieces = [request_pieces[0], response2]
    orchestrator._prompt_normalizer.send_prompt_batch_to_target_async = AsyncMock(
        return_value=[piece.to_prompt_request_response() for piece in request_pieces]
    )

    with patch.object(orchestrator._memory, "get_prompt_request_pieces", return_value=request_pieces):
        await orchestrator.send_prompts_async(prompt_list=[request_pieces[0].original_value])

    # Assert scoring amount is appropriate (all prompts not scored again)
    # and that the last call to the function was with the expected response object
    assert scorer.score_async.call_count == num_conversations + 1
    scorer.score_async.assert_called_with(request_response=response2, task=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("num_prompts", [2, 20])
@pytest.mark.parametrize("max_rpm", [30])
async def test_max_requests_per_minute_delay(patch_central_database, num_prompts: int, max_rpm: int):
    mock_target = MockPromptTarget(rpm=max_rpm)
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target, batch_size=1)

    prompt_list = []
    for n in range(num_prompts):
        prompt_list.append("test")

    start = time.time()
    await orchestrator.send_prompts_async(prompt_list=prompt_list)
    end = time.time()

    assert (end - start) > (60 / max_rpm * num_prompts)


def test_orchestrator_sets_target_memory(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)
    assert orchestrator._memory is mock_target._memory


def test_send_prompt_to_identifier(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)

    d = orchestrator.get_identifier()
    assert d["id"]
    assert d["__type__"] == "PromptSendingOrchestrator"
    assert d["__module__"] == "pyrit.orchestrator.single_turn.prompt_sending_orchestrator"


def test_orchestrator_get_memory(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)

    request = PromptRequestPiece(
        role="user",
        original_value="test",
        orchestrator_identifier=orchestrator.get_identifier(),
    ).to_prompt_request_response()

    orchestrator._memory.add_request_response_to_memory(request=request)

    entries = orchestrator.get_memory()
    assert entries
    assert len(entries) == 1


@pytest.mark.asyncio
async def test_orchestrator_send_prompts_async_with_env_local_memory_labels(mock_target: MockPromptTarget):

    with patch(
        "os.environ.get",
        side_effect=lambda key, default=None: '{"op_name": "dummy_op"}' if key == "GLOBAL_MEMORY_LABELS" else default,
    ):
        orchestrator = PromptSendingOrchestrator(objective_target=mock_target)
        await orchestrator.send_prompts_async(prompt_list=["hello"])
        assert mock_target.prompt_sent == ["hello"]

        expected_labels = {"op_name": "dummy_op"}

        entries = orchestrator.get_memory()
        assert len(entries) == 2
        assert entries[0].labels == expected_labels
        assert entries[1].labels == expected_labels


@pytest.mark.asyncio
async def test_orchestrator_send_prompts_async_with_memory_labels(mock_target: MockPromptTarget):
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target)
    new_labels = {"op_name": "op1", "username": "name1"}
    await orchestrator.send_prompts_async(prompt_list=["hello"], memory_labels=new_labels)
    assert mock_target.prompt_sent == ["hello"]

    expected_labels = {"op_name": "op1", "username": "name1"}
    entries = orchestrator.get_memory()
    assert len(entries) == 2
    assert entries[0].labels == expected_labels
    assert entries[1].labels == expected_labels


@pytest.mark.asyncio
async def test_orchestrator_combine_memory_labels(mock_target: MockPromptTarget):
    with patch(
        "os.environ.get",
        side_effect=lambda key, default=None: '{"op_name": "dummy_op"}' if key == "GLOBAL_MEMORY_LABELS" else default,
    ):
        orchestrator = PromptSendingOrchestrator(objective_target=mock_target)
        new_labels = {"op_name": "op2", "username": "dummy_name"}
        await orchestrator.send_prompts_async(prompt_list=["hello"], memory_labels=new_labels)
        assert mock_target.prompt_sent == ["hello"]

        expected_labels = {"op_name": "op2", "username": "dummy_name"}
        entries = orchestrator.get_memory()
        assert len(entries) == 2
        assert entries[0].labels == expected_labels


@pytest.mark.asyncio
async def test_orchestrator_get_score_memory(mock_target: MockPromptTarget):
    scorer = AsyncMock()
    orchestrator = PromptSendingOrchestrator(objective_target=mock_target, scorers=[scorer])

    request = PromptRequestPiece(
        role="user",
        original_value="dummytest",
        orchestrator_identifier=orchestrator.get_identifier(),
    )

    score = Score(
        score_type="float_scale",
        score_value=str(1),
        score_value_description=None,
        score_category="mock",
        score_metadata=None,
        score_rationale=None,
        scorer_class_identifier=orchestrator.get_identifier(),
        prompt_request_response_id=request.id,
    )

    orchestrator._memory.add_request_pieces_to_memory(request_pieces=[request])
    orchestrator._memory.add_scores_to_memory(scores=[score])
    with patch.object(orchestrator._memory, "get_prompt_request_pieces", return_value=[request]):
        scores = orchestrator.get_score_memory()
        assert len(scores) == 1
        assert scores[0].prompt_request_response_id == request.id


@pytest.mark.parametrize("orchestrator_count", [10, 100])
def test_orchestrator_unique_id(orchestrator_count: int):
    orchestrator_ids = set()
    duplicate_found = False
    for n in range(orchestrator_count):
        id = PromptSendingOrchestrator(objective_target=MagicMock()).get_identifier()["id"]

        if id in orchestrator_ids:
            duplicate_found = True
            break

        orchestrator_ids.add(id)

    assert not duplicate_found


def test_prepare_conversation_with_prepended_conversation():
    with patch("pyrit.orchestrator.single_turn.prompt_sending_orchestrator.uuid.uuid4") as mock_uuid:

        mock_uuid.return_value = "mocked-uuid"
        objective_target_mock = MagicMock()
        memory_mock = MagicMock()
        orchestrator = PromptSendingOrchestrator(objective_target=objective_target_mock)
        orchestrator._memory = memory_mock
        prepended_conversation = [PromptRequestResponse(request_pieces=[MagicMock(conversation_id=None)])]
        orchestrator.set_prepended_conversation(prepended_conversation=prepended_conversation)

        conversation_id = orchestrator._prepare_conversation()

        assert conversation_id == "mocked-uuid"
        for request in prepended_conversation:
            for piece in request.request_pieces:
                assert piece.conversation_id == "mocked-uuid"

        memory_mock.add_request_response_to_memory.assert_called_with(request=prepended_conversation[0])


def test_prepare_conversation_without_prepended_conversation():
    objective_target_mock = MagicMock()
    orchestrator = PromptSendingOrchestrator(objective_target=objective_target_mock)
    memory_mock = MagicMock()

    orchestrator._memory = memory_mock
    conversation_id = orchestrator._prepare_conversation()

    assert not conversation_id
    memory_mock.add_request_response_to_memory.assert_not_called()

# 1. add test for zero retries
#    - should not call the retry logic
# @pytest.mark.asyncio
# async def test_zero_retries():
#     prompt_target_mock = MagicMock()
#     orchestrator = PromptSendingOrchestrator(prompt_target=prompt_target_mock)
    
#     await orchestrator.send_prompts_async(prompt_list=["hello"], max_retries_on_refusal=0)
#     orchestrator._get_should_retry_score_async.assert_not_called()



# 2. add test for one retry
#    a) - mock response as a refusal
#       - check that one retry is happening
#    b) - mock response a non-refusal
#       - check no retry is happening

# 3. capture error if scorer is not true_false

# 4. user does not specify scorer, should default to SelfAskRefusalScorer

# 5. test for _get_should_retry_score_async, task should not be None if prompt request piece is text
#    a) scorer.score_async.assert_called_with(request_response=response2, task=None)
#    b) check test=None when the prompt request piece is image