## client to interact with the garage data lake 
import boto3
from botocore.config import Config
import os

def init_client():
    return boto3.client(
        "s3",
        endpoint_url="http://localhost:3900",
        region_name="garage",
        aws_access_key_id=os.environ["GARAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["GARAGE_SECRET_KEY"],
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"}
        )
    )
def read_from_lake():
    pass 

def write_to_lake(s3, filename, bucket, key):
    s3.upload_file(
        Filename=filename,
        Bucket=bucket,
        Key=key
    )