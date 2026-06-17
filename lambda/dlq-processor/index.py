"""
DLQ Processor - Lambda Handler

Processes failed records from Dead Letter Queue by re-invoking the unified stream processor.
Implements exponential backoff with max retry limit.

Architecture: DLQ (SQS) → Lambda (retry) → Unified Stream Processor → Iceberg

Key insight: With a single unified stream processor, DLQ logic is simplified:
- No need to determine which stream processor Lambda to invoke
- Just re-invoke the known unified stream processor function
- All retries route to the same Lambda, maintaining consistency
"""

import json
import os
from typing import Any, Dict
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

lambda_client = boto3.client('lambda')

STREAM_PROCESSOR_FUNCTION_NAME = os.environ['STREAM_PROCESSOR_FUNCTION_NAME']
MAX_RETRIES = int(os.environ.get('MAX_RETRIES', '3'))


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Process failed records from DLQ and retry by re-invoking the unified stream processor.

    SQS event format:
    - Records: List of SQS messages
    - Each message body contains the original Lambda event that failed
    """
    logger.info(f"Processing {len(event.get('Records', []))} DLQ messages")
    logger.info(f"Target stream processor: {STREAM_PROCESSOR_FUNCTION_NAME}")

    successful = 0
    failed = 0

    for sqs_record in event.get('Records', []):
        try:
            # Parse SQS message body (contains original Lambda event)
            message_body = json.loads(sqs_record['body'])

            # SQS increments ApproximateReceiveCount automatically on each delivery.
            # Custom RetryCount attributes are immutable and cannot be used for retry tracking.
            retry_count = int(
                sqs_record.get('attributes', {}).get('ApproximateReceiveCount', '1')
            )

            if retry_count > MAX_RETRIES:
                logger.error(f"Max retries ({MAX_RETRIES}) exceeded, giving up on DLQ message")
                failed += 1
                continue

            # Validate original event has Records
            if 'Records' not in message_body or len(message_body['Records']) == 0:
                logger.warning("No Records found in DLQ message body")
                failed += 1
                continue

            ddb_records = message_body['Records']
            logger.info(f"Retrying {len(ddb_records)} DynamoDB records")

            logger.info(
                f"Retrying {STREAM_PROCESSOR_FUNCTION_NAME} "
                f"(attempt {retry_count}/{MAX_RETRIES}, "
                f"re-delivery pacing handled by SQS visibility timeout)"
            )

            # Re-invoke the unified stream processor synchronously with original event
            try:
                response = lambda_client.invoke(
                    FunctionName=STREAM_PROCESSOR_FUNCTION_NAME,
                    InvocationType='RequestResponse',  # Synchronous
                    Payload=json.dumps(message_body),
                )

                # Check for Lambda-level errors (unhandled exception, timeout, OOM)
                if response.get('FunctionError'):
                    error_msg = f"Stream processor function error: {response['FunctionError']}"
                    logger.warning(error_msg, exc_info=True)
                    failed += 1
                    raise Exception(error_msg)

                # Check the function's own response body status code
                payload = json.loads(response['Payload'].read())
                payload_status = payload.get('statusCode', 200)
                if payload_status != 200:
                    error_msg = f"Stream processor returned statusCode {payload_status}"
                    logger.error(error_msg)  # nosemgrep: logging-error-without-handling — DLQ boundary handler; ERROR ensures CloudWatch alarm visibility
                    failed += 1
                    raise Exception(error_msg)

                logger.info(f"Successfully retried {STREAM_PROCESSOR_FUNCTION_NAME}")
                successful += 1

            except lambda_client.exceptions.ResourceNotFoundException:
                logger.error(f"Stream processor Lambda function not found: {STREAM_PROCESSOR_FUNCTION_NAME}")  # nosemgrep: logging-error-without-handling — DLQ boundary handler; ERROR ensures CloudWatch alarm visibility
                failed += 1

                # If not at max retries, raise exception to return to DLQ
                if retry_count + 1 < MAX_RETRIES:
                    raise Exception(f"Stream processor function not found, will retry")

        except Exception as e:
            logger.error(f"Error processing DLQ message: {str(e)}", exc_info=True)  # nosemgrep: logging-error-without-handling — DLQ boundary handler; ERROR ensures CloudWatch alarm visibility
            failed += 1

            # Extract retry count and conditionally re-raise
            retry_count = int(
                sqs_record.get('attributes', {}).get('ApproximateReceiveCount', '1')
            )

            # Re-raise to return message to DLQ for another retry
            if retry_count + 1 < MAX_RETRIES:
                raise

    logger.info(f"DLQ processing complete: {successful} successful, {failed} failed")

    return {
        'statusCode': 200,
        'successful': successful,
        'failed': failed
    }
