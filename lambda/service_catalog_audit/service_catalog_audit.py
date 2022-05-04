"""
This script is triggered by EventBridge when an account within this AWS Organization
sends an event of a Service Catalog product that is provisioned or terminated, then
populates the audit DynamoDB table.

Additionally, SQS dead letter queue will trigger this Lambda with events that failed
to send to the custom event bus in the hub account.
"""
import os
import logging
import json
import boto3
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
REGION = os.environ.get('PRIMARY_REGION')
table_name = os.environ.get('AUDIT_TABLE')
session = boto3.session.Session()
client = session.client(
    service_name='dynamodb',
    region_name=REGION
)


def get_item(provisioned_product_id: str, product_region: str, product_account_id: str) -> dict:
    """Gets an item from a DDB table given an provisionedProductId"""
    try:
        response = client.get_item(
            TableName=table_name,
            Key={
                'provisionedProductId': {
                    'S': provisioned_product_id
                },
                'accountIdRegion':{
                    'S': f"{product_account_id}-{product_region}"
                }
            }
        )
    except ClientError as ex:
        if ex.response['Error']['Code'] == 'ResourceNotFoundException':
            LOGGER.exception('Unable to find provisionedProductId %s, will create.',
                                provisioned_product_id)
            raise ex
        elif ex.response['Error']['Code'] == 'RequestLimitExceeded':
            raise ex
        elif ex.response['Error']['Code'] == 'ProvisionedThroughputExceededException':
            raise ex
        else:
            raise ex
    else:
        if len(response) == 1:
            return None
        return response['Item']


def put_product(event: dict) -> dict:
    """Inserts or updates a product into a DDB table"""

    attribute_dict = _return_create_attribute_dict(event)
    product_region = event['region']
    product_account_id = event['detail']['userIdentity']['accountId']
    provisioned_product_id = event['detail']['responseElements']['recordDetail']['provisionedProductId']

    try:
        LOGGER.info("Product %s was launched in %s", provisioned_product_id, product_account_id)
        response = client.update_item(
            TableName=table_name,
            Key={
                'provisionedProductId':{
                    'S': provisioned_product_id
                },
                'accountIdRegion':{
                    'S': f"{product_account_id}-{product_region}"
                }
            },
            AttributeUpdates=attribute_dict
        )
        LOGGER.info("Successfully added product %s to %s", provisioned_product_id, table_name)
        return response
    except ClientError as ex:
        LOGGER.exception('Unable to insert or update provisionedProductId %s.',
                            provisioned_product_id)
        raise ex


def update_product(event: dict) -> dict:
    """Updates an existing product with updated time"""

    event_time = event['detail']['eventTime']
    product_region = event['region']
    product_account_id = event['detail']['userIdentity']['accountId']
    provisioned_product_id = event['detail']['responseElements']['recordDetail']['provisionedProductId']
    record_detail = event['detail']['responseElements']['recordDetail']

    try:
        LOGGER.info("Product %s was updated in %s", provisioned_product_id, product_account_id)
        response = client.update_item(
            TableName=table_name,
            Key={
                'provisionedProductId':{
                    'S': provisioned_product_id
                },
                'accountIdRegion':{
                    'S': f"{product_account_id}-{product_region}"
                }
            },
            UpdateExpression="SET #provisioningArtifactId=:pai, #provisioningArtifactName=:pan, #recordId=:ri, #recordType=:rt, #updatedTime=:ut",
            ExpressionAttributeNames={
                '#provisioningArtifactId': 'provisioningArtifactId',
                '#provisioningArtifactName':  'provisioningArtifactName',
                '#recordId': 'recordId',
                '#recordType': 'recordType',
                '#updatedTime': 'updatedTime'
            },
            ExpressionAttributeValues={
                ':pai': {'S': record_detail['provisioningArtifactId']},
                ':pan': {'S': record_detail['provisioningArtifactName']},
                ':ri': {'S': record_detail['recordId']},
                ':rt': {'S': record_detail['recordType']},
                ':ut': {'S': event_time}
            },
            ReturnValues="UPDATED_NEW"
        )
        LOGGER.info("Successfully updated product %s", provisioned_product_id)
        return response
    except ClientError as ex:
        LOGGER.exception('Unable to update provisionedProductId %s.',
                            provisioned_product_id)
        raise ex


def terminate_product(event: dict) -> dict:
    """Updates an existing product with termination values"""

    event_time = event['detail']['eventTime']
    product_region = event['region']
    product_account_id = event['detail']['userIdentity']['accountId']
    provisioned_product_id = event['detail']['responseElements']['recordDetail']['provisionedProductId']
    record_detail = event['detail']['responseElements']['recordDetail']

    try:
        LOGGER.info("Product %s was terminated in %s", provisioned_product_id, product_account_id)
        response = client.update_item(
            TableName=table_name,
            Key={
                'provisionedProductId':{
                    'S': provisioned_product_id
                },
                'accountIdRegion':{
                    'S': f"{product_account_id}-{product_region}"
                }
            },
            UpdateExpression="SET #status=:s, #updatedTime=:ut, #recordType=:rt",
            ExpressionAttributeNames={
                '#recordType': 'recordType',
                '#status': 'status',
                '#updatedTime': 'updatedTime'
            },
            ExpressionAttributeValues={
                ':rt': {'S': record_detail['recordType']},
                ':s': {'S': "TERMINATED"},
                ':ut': {'S': event_time}
            },
            ReturnValues="UPDATED_NEW"
        )
        LOGGER.info("Successfully updated product %s to TERMINATED", provisioned_product_id)
        return response
    except ClientError as ex:
        LOGGER.exception('Unable to update provisionedProductId %s.',
                            provisioned_product_id)
        raise ex


def process_item(event: dict):
    """Process incomming event from EventBridge or SQS"""

    event_name = event['detail']['eventName']
    product_region = event['region']
    product_account_id = event['detail']['userIdentity']['accountId']
    provisioned_product_id = event['detail']['responseElements']['recordDetail']['provisionedProductId']

    if event_name in ["ProvisionProduct"]:
        put_product(event)
    elif event_name == "UpdateProvisionedProduct":
        if get_item(provisioned_product_id, product_region, product_account_id):
            update_product(event)
        else:
            LOGGER.warning("provisionedProductId %s was not found in %s, so won't update anything",
                            provisioned_product_id, table_name)
    elif event_name == "TerminateProvisionedProduct":
        if get_item(provisioned_product_id, product_region, product_account_id):
            terminate_product(event)
        else:
            LOGGER.warning("provisionedProductId %s was not found in %s, so won't update anything",
                            provisioned_product_id, table_name)
    else:
        LOGGER.exception("Unrecognized event_name %s, skipping.")


def _return_create_attribute_dict(event: dict) -> dict:
    """Return a dict for Dynamodb AttributeUpdates field populated with create responseElements"""

    attribute_dict = {}
    event_time = event['detail']['eventTime']
    product_region = event['region']
    product_account_id = event['detail']['userIdentity']['accountId']
    record_detail = event['detail']['responseElements']['recordDetail']
    user_identity = event['detail']['userIdentity']['arn']

    for item in record_detail.keys():
        # Handling provisionedProductId in put_item method
        if item in ["provisionedProductId", "createdTime", "updatedTime"]:
            continue
        attribute_dict[item] = {
            "Action": "PUT",
            "Value": {
                "S": str(record_detail[item])
            }
        }

    # Assumed IAM user or role ARN used to create product
    attribute_dict['userIdentityArn'] = {
        "Action": "PUT",
        "Value": {
            "S": user_identity
        }
    }

    # AWS account the product is created in
    attribute_dict['accountId'] = {
        "Action": "PUT",
        "Value": {
            "S": product_account_id
        }
    }

    # AWS region the product is created in
    attribute_dict['region'] = {
        "Action": "PUT",
        "Value": {
            "S": product_region
        }
    }

    # Setting createdTime and updatedTime to eventTime so date format is same
    # for tracking future update times of the product
    attribute_dict['createdTime'] = {
        "Action": "PUT",
        "Value": {
            "S": event_time
        }
    }

    attribute_dict['updatedTime'] = {
        "Action": "PUT",
        "Value": {
            "S": event_time
        }
    }

    return attribute_dict


def delete_sqs_message(receipt_handle: str) -> dict:
    """Delete the processed message in the SQS queue"""

    sqs_url = os.getenv('SQS_DLQ')
    sqs_client = boto3.client('sqs', region_name=REGION)

    try:
        response = sqs_client.delete_message(
            QueueUrl=sqs_url,
            ReceiptHandle=receipt_handle
        )
    except ClientError as exc:
        LOGGER.error("Failed to delete message from SQS queue: %s", sqs_url)
        LOGGER.error(exc.response['Error']['Message'])
        raise
    else:
        LOGGER.info("SQS message deleted from %s! Receipt Handle: %s", sqs_url, receipt_handle)
    return response


def lambda_handler(event: dict, context: dict):
    LOGGER.info(event)

    if event.get("Records"):
        LOGGER.info("Processing event(s) from DLQ")
        for i, j in enumerate(event["Records"]):
            receipt_handle = event["Records"][i]["receiptHandle"]
            event = json.loads(event["Records"][i]["body"])
            process_item(event)
            LOGGER.info("Processed event from DLQ successfully; deleting message.")
            delete_sqs_message(receipt_handle)
    else:
        process_item(event)
