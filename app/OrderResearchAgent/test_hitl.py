"""
Human-in-the-Loop test for AgentCore Harness inline functions.

Usage:
    export HARNESS_ARN=<arn>
    python3 test_hitl.py

    python3 test_hitl.py --harness-arn <arn>
    python3 test_hitl.py --prompt "Your custom prompt"
"""

import boto3
import json
import uuid
import os
import argparse

DEFAULT_PROMPT = (
    "A customer needs a $200 refund for order ORD-55555 because they received "
    "a damaged product. Try to process it, and if you can't, escalate for manager approval."
)

def parse_args():
    parser = argparse.ArgumentParser(description="Test HITL inline functions with AgentCore Harness")
    parser.add_argument("--harness-arn", default=os.environ.get("HARNESS_ARN"),
                        help="Harness ARN (default: HARNESS_ARN env var)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt to send to the agent")
    return parser.parse_args()

def parse_stream(response):
    """Parse streaming response. Returns tool call info and stop reason."""
    tool_use_id = None
    tool_name = None
    tool_input_chunks = ""
    stop_reason = None
    current_block_is_tool = False

    for event in response["stream"]:
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tool_use_id = start["toolUse"]["toolUseId"]
                tool_name = start["toolUse"].get("name")
                tool_input_chunks = ""
                current_block_is_tool = True
            else:
                current_block_is_tool = False
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                print(delta["text"], end="", flush=True)
            elif "toolUse" in delta and current_block_is_tool:
                tool_input_chunks += delta["toolUse"].get("input", "")
        elif "contentBlockStop" in event:
            current_block_is_tool = False
        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")

    print()

    tool_input = None
    if tool_input_chunks:
        try:
            tool_input = json.loads(tool_input_chunks)
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            try:
                tool_input, _ = decoder.raw_decode(tool_input_chunks)
            except json.JSONDecodeError:
                tool_input = {}

    return {
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "stop_reason": stop_reason,
    }

def main():
    args = parse_args()
    harness_arn = args.harness_arn
    if not harness_arn:
        print("Error: HARNESS_ARN not set. Export it or pass --harness-arn.")
        exit(1)

    client = boto3.client("bedrock-agentcore")
    session_id = str(uuid.uuid4())

    # Step 1: Send the prompt
    response = client.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": args.prompt}]}],
    )
    result = parse_stream(response)

    # Step 2: If the agent paused on an inline function, prompt for approval
    if result["stop_reason"] == "tool_use" and result["tool_name"] == "approve_exception":
        print(f"\nAgent called: {result['tool_name']}")
        print(f"Input: {json.dumps(result['tool_input'], indent=2)}")
        approval = input("\nApprove this refund? (yes/no): ").strip().lower()

        if approval in ("yes", "y"):
            tool_result = json.dumps({"approved": True, "approver": "manager-jane"})
        else:
            tool_result = json.dumps({"approved": False, "reason": "Manager denied"})

        # Step 3: Resume with toolUse + toolResult
        tool_input_value = result["tool_input"] if isinstance(result["tool_input"], dict) else {}
        response = client.invoke_harness(
            harnessArn=harness_arn,
            runtimeSessionId=session_id,
            messages=[
                {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": result["tool_use_id"], "name": result["tool_name"], "input": tool_input_value}}
                ]},
                {"role": "user", "content": [
                    {"toolResult": {"toolUseId": result["tool_use_id"], "content": [{"text": tool_result}], "status": "success"}}
                ]},
            ],
        )
        parse_stream(response)
    else:
        print(f"\nAgent completed. Stop reason: {result['stop_reason']}")

if __name__ == "__main__":
    main()
