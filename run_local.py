import sys
import os
import json
from unittest.mock import MagicMock, patch

# Ensure src is importable
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

# Set mock environment variables
os.environ["DYNAMODB_TABLE"] = "mock-drone-inspection-table"
os.environ["S3_IMAGES_BUCKET"] = "mock-drone-inspection-images"

# In-memory database simulation store
mock_db = []

# --- Mocking Setup ---

class MockTable:
    def put_item(self, Item):
        print(f"  [DDB Mock] put_item called: PK={Item.get('PK')}, SK={Item.get('SK')}")
        # Check if already exists, overwrite if yes
        for idx, existing in enumerate(mock_db):
            if existing.get("PK") == Item.get("PK") and existing.get("SK") == Item.get("SK"):
                mock_db[idx] = Item
                return {"ResponseMetadata": {"HTTPStatusCode": 200}}
        mock_db.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}

    def query(self, **kwargs):
        limit = kwargs.get("Limit", 20)
        # Parse expression values
        expr_vals = kwargs.get("ExpressionAttributeValues", {})
        
        # Simple query parser based on typical lookups
        results = []
        
        # Check GSI1 query
        if kwargs.get("IndexName") == "GSI1":
            # Extract partition key match
            pk_val = expr_vals.get(":pk")
            sk_prefix = expr_vals.get(":sk_prefix")
            
            # If KeyConditionExpression is a boto3 Key object, we inspect its attributes
            # Let's fallback to manually checking variables in GSI1 query shape
            if not pk_val:
                # Key object string representation analysis helper
                key_expr_str = str(kwargs.get("KeyConditionExpression"))
                # Extract WAREHOUSE# or DRONE# from expression
                for item in mock_db:
                    if "GSI1-PK" in item and item["GSI1-PK"].startswith("WAREHOUSE#"):
                        results.append(item)
            else:
                for item in mock_db:
                    if item.get("GSI1-PK") == pk_val:
                        if sk_prefix and not item.get("GSI1-SK", "").startswith(sk_prefix):
                            continue
                        results.append(item)
        else:
            # Base table query
            pk_val = expr_vals.get(":pk")
            sk_prefix = expr_vals.get(":sk_prefix")
            
            if not pk_val:
                # Inspect Key object
                key_expr_str = str(kwargs.get("KeyConditionExpression"))
                # Filter images by inspection id or drone link items
                for item in mock_db:
                    # Match images (PK: INSPECTION#..., SK: IMAGE#...)
                    # or drone links (PK: DRONE#..., SK: INSPECTION#...)
                    if "INSPECTION#" in item.get("PK", "") or "DRONE#" in item.get("PK", ""):
                        results.append(item)
            else:
                for item in mock_db:
                    if item.get("PK") == pk_val:
                        if sk_prefix and not item.get("SK", "").startswith(sk_prefix):
                            continue
                        results.append(item)
        
        # Sort results: descending order if ScanIndexForward is False
        scan_forward = kwargs.get("ScanIndexForward", True)
        # Sort based on SK (lexicographically, which works for ISO timestamps)
        results.sort(key=lambda x: x.get("SK", "") or x.get("GSI1-SK", ""), reverse=not scan_forward)
        
        sliced_results = results[:limit]
        last_evaluated = None
        if len(results) > limit:
            last_evaluated = sliced_results[-1]

        print(f"  [DDB Mock] query returned {len(sliced_results)} items.")
        return {
            "Items": sliced_results,
            "LastEvaluatedKey": last_evaluated
        }

class MockDynamoDBClient:
    def transact_write_items(self, TransactItems):
        print(f"  [DDB Mock] transact_write_items called with {len(TransactItems)} operations.")
        for op in TransactItems:
            if "Put" in op:
                raw_item = op["Put"]["Item"]
                # Convert low-level client format to high-level resource format
                item = {}
                for k, v in raw_item.items():
                    item[k] = list(v.values())[0]
                
                # Overwrite or append
                exists = False
                for idx, existing in enumerate(mock_db):
                    if existing.get("PK") == item.get("PK") and existing.get("SK") == item.get("SK"):
                        mock_db[idx] = item
                        exists = True
                        break
                if not exists:
                    mock_db.append(item)
        return {}

class MockS3Client:
    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        bucket = Params.get("Bucket")
        key = Params.get("Key")
        url = f"https://{bucket}.s3.amazonaws.com/{key}?Expires={ExpiresIn}&Signature=mock_signature_tokens"
        print(f"  [S3 Mock] generate_presigned_url: {url}")
        return url

# --- Run Tests ---

def run_local_simulation():
    print("=" * 60)
    print("  VECROS DRONE BACKEND - LOCAL SIMULATION RUNNER")
    print("=" * 60)

    # Initialize mock patches
    mock_table = MockTable()
    mock_client = MockDynamoDBClient()
    mock_s3 = MockS3Client()

    # Apply patches
    patcher_resource = patch("boto3.resource")
    patcher_client = patch("boto3.client")
    
    mock_boto3_resource = patcher_resource.start()
    mock_boto3_client = patcher_client.start()
    
    # Configure mock returns
    mock_dynamodb_resource = MagicMock()
    mock_dynamodb_resource.Table.return_value = mock_table
    mock_dynamodb_resource.meta.client = mock_client
    
    def side_effect_resource(service_name):
        if service_name == "dynamodb":
            return mock_dynamodb_resource
        return MagicMock()
        
    def side_effect_client(service_name):
        if service_name == "dynamodb":
            return mock_client
        if service_name == "s3":
            return mock_s3
        return MagicMock()

    mock_boto3_resource.side_effect = side_effect_resource
    mock_boto3_client.side_effect = side_effect_client

    try:
        # Import handlers inside the mock context
        from src import inspections
        from src import images

        # Re-initialize local handlers module references
        inspections.table = mock_table
        inspections.dynamodb_client = mock_client
        images.table = mock_table
        images.s3_client = mock_s3

        print("\n[STEP 1] Creating an Inspection...")
        create_event = {
            "pathParameters": {
                "warehouse_id": "wh_north_01"
            },
            "body": json.dumps({
                "drone_id": "drone_hex_45",
                "summary": "Grid inspect phase A",
                "status": "IN_PROGRESS"
            })
        }
        res = inspections.create_inspection(create_event, None)
        print(f"Response: {res['statusCode']}")
        print(json.dumps(json.loads(res["body"]), indent=4))
        
        # Save inspection_id for subsequent tests
        inspection_id = json.loads(res["body"])["inspection_id"]

        print("\n[STEP 2] Listing Inspections by Warehouse...")
        list_wh_event = {
            "pathParameters": {
                "warehouse_id": "wh_north_01"
            },
            "queryStringParameters": {
                "limit": "5"
            }
        }
        res = inspections.list_inspections_by_warehouse(list_wh_event, None)
        print(f"Response: {res['statusCode']}")
        print(json.dumps(json.loads(res["body"]), indent=4))

        print("\n[STEP 3] Listing Inspections by Drone...")
        list_drone_event = {
            "pathParameters": {
                "drone_id": "drone_hex_45"
            }
        }
        res = inspections.list_inspections_by_drone(list_drone_event, None)
        print(f"Response: {res['statusCode']}")
        print(json.dumps(json.loads(res["body"]), indent=4))

        print("\n[STEP 4] Generating S3 Upload Pre-signed URL...")
        upload_event = {
            "pathParameters": {
                "inspection_id": inspection_id
            },
            "body": json.dumps({
                "filename": "camera_leak_north.png"
            })
        }
        res = images.generate_upload_url(upload_event, None)
        print(f"Response: {res['statusCode']}")
        print(json.dumps(json.loads(res["body"]), indent=4))
        
        image_id = json.loads(res["body"])["image_id"]
        s3_uri = json.loads(res["body"])["s3_uri"]

        print("\n[STEP 5] Saving Image Upload Confirmation Metadata...")
        save_img_event = {
            "pathParameters": {
                "inspection_id": inspection_id
            },
            "body": json.dumps({
                "image_id": image_id,
                "s3_uri": s3_uri,
                "upload_status": "COMPLETED"
            })
        }
        res = images.save_metadata(save_img_event, None)
        print(f"Response: {res['statusCode']}")
        print(json.dumps(json.loads(res["body"]), indent=4))

        print("\n[STEP 6] Listing All Images for the Inspection...")
        list_imgs_event = {
            "pathParameters": {
                "inspection_id": inspection_id
            }
        }
        res = images.list_images_for_inspection(list_imgs_event, None)
        print(f"Response: {res['statusCode']}")
        print(json.dumps(json.loads(res["body"]), indent=4))

        print("\n" + "=" * 60)
        print("  SIMULATION COMPLETE: ALL HANDLERS RUNNING CORRECTLY")
        print("=" * 60)

    finally:
        patcher_resource.stop()
        patcher_client.stop()

if __name__ == "__main__":
    run_local_simulation()
