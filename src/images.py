import base64
import datetime
import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE")
S3_IMAGES_BUCKET = os.environ.get("S3_IMAGES_BUCKET")

if not DYNAMODB_TABLE:
    logger.critical("DYNAMODB_TABLE environment variable is missing!")
if not S3_IMAGES_BUCKET:
    logger.critical("S3_IMAGES_BUCKET environment variable is missing!")

dynamodb = boto3.resource("dynamodb")
s3_client = boto3.client("s3")
table = dynamodb.Table(DYNAMODB_TABLE) if DYNAMODB_TABLE else None

CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
    "Access-Control-Allow-Methods": "DELETE,GET,HEAD,OPTIONS,PATCH,POST,PUT",
}

class DecimalEncoder(json.JSONEncoder):
    """Custom JSON encoder to handle DynamoDB Decimal types without serialization errors."""
    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
        return super(DecimalEncoder, self).default(obj)

def api_response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    """Formats a standardized HTTP response format for AWS API Gateway Proxy."""
    return {
        "statusCode": status_code,
        "headers": CORS_HEADERS,
        "body": json.dumps(body, cls=DecimalEncoder)
    }

def encode_cursor(key: Optional[Dict[str, Any]]) -> str:
    """Encodes a DynamoDB LastEvaluatedKey dictionary to a URL-safe Base64 string."""
    if not key:
        return ""
    try:
        json_str = json.dumps(key, cls=DecimalEncoder)
        return base64.urlsafe_b64encode(json_str.encode("utf-8")).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to encode pagination cursor: {str(e)}")
        return ""

def decode_cursor(cursor_str: Optional[str]) -> Optional[Dict[str, Any]]:
    """Decodes a URL-safe Base64 cursor string back to a DynamoDB ExclusiveStartKey dictionary."""
    if not cursor_str:
        return None
    try:
        decoded_bytes = base64.urlsafe_b64decode(cursor_str.encode("utf-8"))
        return json.loads(decoded_bytes.decode("utf-8"))
    except Exception as e:
        logger.error(f"Failed to decode pagination cursor '{cursor_str}': {str(e)}")
        raise ValueError("Invalid pagination cursor token format")

def generate_upload_url(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Generates an S3 pre-signed upload URL for put_object, tracks metadata record in DynamoDB,
    and sets the initial status to 'PENDING_UPLOAD'.
    """
    logger.info("Received request to generate upload URL", extra={"event": event})

    if not table or not S3_IMAGES_BUCKET:
        logger.error("Database or S3 Bucket is not properly initialized.")
        return api_response(500, {"error": "Internal infrastructure configuration error."})

    try:
        path_params = event.get("pathParameters") or {}
        inspection_id = path_params.get("inspection_id")
        
        if not inspection_id or not inspection_id.strip():
            logger.warning("Validation failed: Missing or empty 'inspection_id' in path parameters.")
            return api_response(400, {"error": "Missing or invalid 'inspection_id' in path."})

        body_str = event.get("body")
        if not body_str:
            logger.warning("Validation failed: Request body is empty.")
            return api_response(400, {"error": "Request body must not be empty."})

        try:
            body = json.loads(body_str)
        except json.JSONDecodeError as decode_err:
            logger.warning(f"Validation failed: Invalid JSON format. {str(decode_err)}")
            return api_response(400, {"error": "Invalid JSON body format."})

        filename = body.get("filename")
        if not filename or not str(filename).strip():
            logger.warning("Validation failed: Missing or empty 'filename' in request payload.")
            return api_response(400, {"error": "Missing or invalid 'filename' in request payload."})

        image_id = str(uuid.uuid4())
        s3_key = f"inspections/{inspection_id}/images/{image_id}_{filename.strip()}"
        s3_uri = f"s3://{S3_IMAGES_BUCKET}/{s3_key}"
        
        logger.info(f"Generating pre-signed URL for S3 key: {s3_key}")
        
        try:
            presigned_url = s3_client.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": S3_IMAGES_BUCKET,
                    "Key": s3_key,
                },
                ExpiresIn=900
            )
        except ClientError as s3_err:
            logger.error(f"S3 Pre-signed URL generation failed: {str(s3_err)}", exc_info=True)
            return api_response(502, {"error": "Failed to generate file upload link from storage."})

        timestamp = datetime.datetime.utcnow().isoformat() + "Z"

        image_metadata = {
            "PK": f"INSPECTION#{inspection_id}",
            "SK": f"IMAGE#{image_id}",
            "image_id": image_id,
            "inspection_id": inspection_id,
            "filename": filename,
            "s3_uri": s3_uri,
            "upload_status": "PENDING_UPLOAD",
            "uploaded_at": timestamp
        }

        try:
            table.put_item(Item=image_metadata)
        except ClientError as ddb_err:
            logger.error(f"DynamoDB metadata write failed: {str(ddb_err)}", exc_info=True)
            return api_response(502, {"error": "Failed to create database record for image metadata."})

        logger.info(f"Successfully generated pre-signed URL & created metadata record for image {image_id}")

        return api_response(200, {
            "image_id": image_id,
            "upload_url": presigned_url,
            "s3_uri": s3_uri,
            "upload_status": "PENDING_UPLOAD",
            "uploaded_at": timestamp
        })

    except Exception as err:
        logger.error(f"Unhandled error in generate_upload_url: {str(err)}", exc_info=True)
        return api_response(500, {"error": "Internal Server Error."})

def save_metadata(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Saves image metadata under an inspection and tracks upload status.
    Writes directly to the base table using parent-child partition grouping.
    """
    logger.info("Received save image metadata request", extra={"event": event})

    if not table:
        logger.error("DynamoDB Table is not initialized.")
        return api_response(500, {"error": "Internal database configuration error."})

    try:
        path_params = event.get("pathParameters") or {}
        inspection_id = path_params.get("inspection_id")
        
        if not inspection_id or not inspection_id.strip():
            logger.warning("Validation failed: Missing or empty 'inspection_id' in path parameters.")
            return api_response(400, {"error": "Missing or invalid 'inspection_id' in path."})

        body_str = event.get("body")
        if not body_str:
            logger.warning("Validation failed: Request body is empty.")
            return api_response(400, {"error": "Request body must not be empty."})

        try:
            body = json.loads(body_str)
        except json.JSONDecodeError as decode_err:
            logger.warning(f"Validation failed: Invalid JSON format. {str(decode_err)}")
            return api_response(400, {"error": "Invalid JSON body format."})

        image_id = body.get("image_id")
        s3_uri = body.get("s3_uri")
        upload_status = body.get("upload_status", "PENDING")

        if not image_id or not str(image_id).strip():
            logger.warning("Validation failed: Missing or empty 'image_id' in request payload.")
            return api_response(400, {"error": "Missing or invalid 'image_id' in request payload."})

        if not s3_uri or not str(s3_uri).strip():
            logger.warning("Validation failed: Missing or empty 's3_uri' in request payload.")
            return api_response(400, {"error": "Missing or invalid 's3_uri' in request payload."})

        timestamp = datetime.datetime.utcnow().isoformat() + "Z"

        image_item = {
            "PK": f"INSPECTION#{inspection_id}",
            "SK": f"IMAGE#{image_id}",
            "image_id": image_id,
            "inspection_id": inspection_id,
            "s3_uri": s3_uri,
            "upload_status": upload_status,
            "timestamp": timestamp
        }

        try:
            table.put_item(Item=image_item)
        except ClientError as ddb_err:
            logger.error(f"DynamoDB write failed: {str(ddb_err)}", exc_info=True)
            return api_response(502, {"error": "Database error writing image metadata."})

        logger.info(f"Successfully saved image metadata for image {image_id} under inspection {inspection_id}")

        return api_response(201, {
            "message": "Image metadata saved successfully.",
            "image_id": image_id,
            "inspection_id": inspection_id,
            "s3_uri": s3_uri,
            "upload_status": upload_status,
            "timestamp": timestamp
        })

    except Exception as err:
        logger.error(f"Unhandled error in save_metadata: {str(err)}", exc_info=True)
        return api_response(500, {"error": "Internal Server Error."})

def list_images_for_inspection(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Lists all image items for a specific inspection.
    Queries the base table partition (INSPECTION#<inspection_id>) filtering SK by "IMAGE#".
    Supports cursor-based pagination.
    """
    logger.info("Received list images request", extra={"event": event})

    if not table:
        logger.error("DynamoDB Table is not initialized.")
        return api_response(500, {"error": "Internal database configuration error."})

    try:
        path_params = event.get("pathParameters") or {}
        inspection_id = path_params.get("inspection_id")

        if not inspection_id or not inspection_id.strip():
            logger.warning("Validation failed: Missing or empty 'inspection_id' in path parameters.")
            return api_response(400, {"error": "Missing or invalid 'inspection_id' in path."})

        query_params = event.get("queryStringParameters") or {}
        cursor = query_params.get("cursor")
        limit_str = query_params.get("limit")

        limit = 50
        if limit_str:
            try:
                limit = int(limit_str)
                if limit <= 0:
                    raise ValueError()
            except ValueError:
                logger.warning(f"Validation failed: Invalid limit: {limit_str}")
                return api_response(400, {"error": "Invalid 'limit'. Must be a positive integer."})

        query_args = {
            "KeyConditionExpression": Key('PK').eq(f"INSPECTION#{inspection_id}") & Key('SK').begins_with("IMAGE#"),
            "Limit": limit
        }

        if cursor:
            try:
                exclusive_start_key = decode_cursor(cursor)
                if exclusive_start_key:
                    query_args["ExclusiveStartKey"] = exclusive_start_key
            except ValueError as val_err:
                logger.warning(f"Validation failed: Invalid cursor format: {str(val_err)}")
                return api_response(400, {"error": "Invalid pagination cursor token."})

        logger.info(f"Querying base table for images under inspection {inspection_id} with limit {limit}")
        try:
            response = table.query(**query_args)
        except ClientError as ddb_err:
            logger.error(f"DynamoDB query failed: {str(ddb_err)}", exc_info=True)
            return api_response(502, {"error": "Failed to fetch images from database."})

        items = response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        next_cursor = encode_cursor(last_key) if last_key else None

        return api_response(200, {
            "images": items,
            "next_cursor": next_cursor
        })

    except Exception as err:
        logger.error(f"Unhandled error in list_images_for_inspection: {str(err)}", exc_info=True)
        return api_response(500, {"error": "Internal Server Error."})
