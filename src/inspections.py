import base64
import datetime
import json
import logging
import os
import uuid
from decimal import Decimal
from typing import Any, Dict, Optional
import boto3
from boto3.dynamodb.conditions import Key

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE")
if not DYNAMODB_TABLE:
    logger.critical("DYNAMODB_TABLE environment variable is missing!")

dynamodb = boto3.resource("dynamodb")
dynamodb_client = dynamodb.meta.client
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

def create_inspection(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Creates an inspection for a warehouse & drone.
    Saves complete metadata and establishes the Drone-Inspection association atomically.
    """
    logger.info("Received create inspection request", extra={"event": event})
    
    if not table:
        logger.error("DynamoDB Table is not initialized.")
        return api_response(500, {"error": "Internal database configuration error."})

    try:
        path_params = event.get("pathParameters") or {}
        warehouse_id = path_params.get("warehouse_id")
        
        if not warehouse_id or not warehouse_id.strip():
            logger.warning("Validation failed: Missing or empty 'warehouse_id' in path parameters.")
            return api_response(400, {"error": "Missing or invalid 'warehouse_id' in path."})
            
        body_str = event.get("body")
        if not body_str:
            logger.warning("Validation failed: Request body is empty.")
            return api_response(400, {"error": "Request body must not be empty."})
            
        try:
            body = json.loads(body_str)
        except json.JSONDecodeError as decode_err:
            logger.warning(f"Validation failed: Invalid JSON format. {str(decode_err)}")
            return api_response(400, {"error": "Invalid JSON body format."})
            
        drone_id = body.get("drone_id")
        if not drone_id or not str(drone_id).strip():
            logger.warning("Validation failed: Missing or empty 'drone_id' in body payload.")
            return api_response(400, {"error": "Missing or invalid 'drone_id' in request payload."})
            
        status = body.get("status", "PENDING")
        summary = body.get("summary", "")
        
        inspection_id = str(uuid.uuid4())
        timestamp = datetime.datetime.utcnow().isoformat() + "Z"
        
        inspection_item = {
            "PK": {"S": f"INSPECTION#{inspection_id}"},
            "SK": {"S": "METADATA"},
            "GSI1-PK": {"S": f"WAREHOUSE#{warehouse_id}"},
            "GSI1-SK": {"S": f"INSPECTION#{timestamp}#{inspection_id}"},
            "inspection_id": {"S": inspection_id},
            "warehouse_id": {"S": warehouse_id},
            "drone_id": {"S": drone_id},
            "timestamp": {"S": timestamp},
            "status": {"S": status},
            "summary": {"S": summary}
        }
        
        drone_link_item = {
            "PK": {"S": f"DRONE#{drone_id}"},
            "SK": {"S": f"INSPECTION#{timestamp}#{inspection_id}"},
            "inspection_id": {"S": inspection_id},
            "warehouse_id": {"S": warehouse_id},
            "status": {"S": status}
        }
        
        dynamodb_client.transact_write_items(
            TransactItems=[
                {
                    "Put": {
                        "TableName": DYNAMODB_TABLE,
                        "Item": inspection_item
                    }
                },
                {
                    "Put": {
                        "TableName": DYNAMODB_TABLE,
                        "Item": drone_link_item
                    }
                }
            ]
        )
        
        logger.info(f"Successfully created inspection ID: {inspection_id}")
        
        return api_response(201, {
            "message": "Inspection created successfully.",
            "inspection_id": inspection_id,
            "warehouse_id": warehouse_id,
            "drone_id": drone_id,
            "timestamp": timestamp,
            "status": status,
            "summary": summary
        })
        
    except Exception as err:
        logger.error(f"Failed to create inspection: {str(err)}", exc_info=True)
        return api_response(500, {"error": "Internal Server Error."})

def list_inspections_by_warehouse(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Retrieves inspections for a warehouse, sorted from latest to oldest.
    Queries GSI1 with cursor-based pagination.
    """
    logger.info("Received list by warehouse request", extra={"event": event})
    
    if not table:
        logger.error("DynamoDB Table is not initialized.")
        return api_response(500, {"error": "Internal database configuration error."})

    try:
        path_params = event.get("pathParameters") or {}
        warehouse_id = path_params.get("warehouse_id")
        
        if not warehouse_id or not warehouse_id.strip():
            logger.warning("Validation failed: Missing or empty 'warehouse_id' in path parameters.")
            return api_response(400, {"error": "Missing or invalid 'warehouse_id' in path."})
            
        query_params = event.get("queryStringParameters") or {}
        cursor = query_params.get("cursor")
        limit_str = query_params.get("limit")
        
        limit = 20
        if limit_str:
            try:
                limit = int(limit_str)
                if limit <= 0:
                    raise ValueError()
            except ValueError:
                logger.warning(f"Validation failed: Invalid limit: {limit_str}")
                return api_response(400, {"error": "Invalid 'limit'. Must be a positive integer."})
                
        query_args = {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key('GSI1-PK').eq(f"WAREHOUSE#{warehouse_id}") & Key('GSI1-SK').begins_with("INSPECTION#"),
            "ScanIndexForward": False,
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
                
        logger.info(f"Querying GSI1 for warehouse {warehouse_id} with limit {limit}")
        response = table.query(**query_args)
        
        items = response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        next_cursor = encode_cursor(last_key) if last_key else None
        
        return api_response(200, {
            "inspections": items,
            "next_cursor": next_cursor
        })
        
    except Exception as err:
        logger.error(f"Failed to list inspections by warehouse: {str(err)}", exc_info=True)
        return api_response(500, {"error": "Internal Server Error."})

def list_inspections_by_drone(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Retrieves inspections for a drone, sorted from latest to oldest.
    Queries base table partition (DRONE#<drone_id>) with cursor-based pagination.
    """
    logger.info("Received list by drone request", extra={"event": event})
    
    if not table:
        logger.error("DynamoDB Table is not initialized.")
        return api_response(500, {"error": "Internal database configuration error."})

    try:
        path_params = event.get("pathParameters") or {}
        drone_id = path_params.get("drone_id")
        
        if not drone_id or not drone_id.strip():
            logger.warning("Validation failed: Missing or empty 'drone_id' in path parameters.")
            return api_response(400, {"error": "Missing or invalid 'drone_id' in path."})
            
        query_params = event.get("queryStringParameters") or {}
        cursor = query_params.get("cursor")
        limit_str = query_params.get("limit")
        
        limit = 20
        if limit_str:
            try:
                limit = int(limit_str)
                if limit <= 0:
                    raise ValueError()
            except ValueError:
                logger.warning(f"Validation failed: Invalid limit: {limit_str}")
                return api_response(400, {"error": "Invalid 'limit'. Must be a positive integer."})
                
        query_args = {
            "KeyConditionExpression": Key('PK').eq(f"DRONE#{drone_id}") & Key('SK').begins_with("INSPECTION#"),
            "ScanIndexForward": False,
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
                
        logger.info(f"Querying base table for drone {drone_id} with limit {limit}")
        response = table.query(**query_args)
        
        items = response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        next_cursor = encode_cursor(last_key) if last_key else None
        
        return api_response(200, {
            "inspections": items,
            "next_cursor": next_cursor
        })
        
    except Exception as err:
        logger.error(f"Failed to list inspections by drone: {str(err)}", exc_info=True)
        return api_response(500, {"error": "Internal Server Error."})
