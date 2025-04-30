from typing import List
from time import perf_counter
import csv
import json
import asyncio
import os
import logging

from fastapi import FastAPI, Form, File, UploadFile, Request, Query, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from pydantic import BaseModel, Field
from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ClientError
import aioboto3
import boto3

import secrets

from typing import Annotated

# Configure logging
logging.basicConfig(
    filename='upload_logs.log',  # Log file path on the server
    level=logging.INFO,          # Log level
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

security = HTTPBasic()

# Load AWS credentials and S3 bucket name from config file
with open('credentials.json') as config_file:
    aws_credentials = json.load(config_file)

AWS_ACCESS_KEY_ID = aws_credentials['AWS_ACCESS_KEY_ID']
AWS_SECRET_ACCESS_KEY = aws_credentials['AWS_SECRET_ACCESS_KEY']
AWS_REGION = aws_credentials['AWS_REGION']
AWS_URL_ENDPOINT = aws_credentials['AWS_URL_ENDPOINT']
API_USERNAME = aws_credentials['API_USERNAME']
API_PASSWORD = aws_credentials['API_PASSWORD']

def authenticate(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
):
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = API_USERNAME.encode('utf-8')
    is_correct_username = secrets.compare_digest(
        current_username_bytes, correct_username_bytes
    )
    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = API_PASSWORD.encode('utf-8')
    is_correct_password = secrets.compare_digest(
        current_password_bytes, correct_password_bytes
    )
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

logger = logging.getLogger(__name__)

tags_metadata = [
    {
        "name": "Data",
        "description": "Operations for data management in the server, uploading and downloading."
    },
    {
        "name": "Deployments",
        "description": "Operations with deployments."
    },
    {
        "name": "Other",
        "description": "Other operations."
    }
]

app = FastAPI(
    title="AMI Data Management API",
    version="1.0.1",
    contact={
        "name": "AMI system team at UKCEH",
        "url": "https://www.ceh.ac.uk/solutions/equipment/automated-monitoring-insects-trap",
        "email": "ami-system@ceh.ac.uk",
    },
    license_info={
        "name": "Apache 2.0",
        "identifier": "MIT",
    },
    openapi_tags=tags_metadata
)

# Set up CORS middleware
origins = [
    "http://localhost",
    "http://localhost:8080",
    "http://127.0.0.1",
    "http://127.0.0.1:8080",
    "https://connect-apps.ceh.ac.uk/ami-data-upload/"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Mount the static directory to serve static files
app.mount("/static", StaticFiles(directory="static"), name="static")

CONCURRENCY_LIMIT = 200 # Adjust this value based on your server capabilities

session = aioboto3.Session()

def load_deployments_info():
    deployments = []
    with open('deployments_info.csv', newline='', encoding='utf-8-sig') as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            deployments.append(row)
    return deployments

deployments_info = load_deployments_info()

valid_countries_names = {d['country'] for d in deployments_info if d['status'] == 'active'}
valid_countries_location_names = {f"{d['country']} - {d['location_name']}" for d in deployments_info
                                  if d['status'] == 'active'}
valid_data_types = {"snapshot_images", "audible_recordings", "ultrasound_recordings", "motion_images"} # DC add "motion images"


class UploadResponse(BaseModel):
    uploaded_files: List[str]


class Deployment(BaseModel):
    country: str
    country_code: str
    location_name: str
    lat: str
    lon: str
    location_id: str
    camera_id: str
    system_id: str
    hardware_id: str
    deployment_id: str
    status: str


class NewDeployment(BaseModel):
    country: str
    country_code: str
    location_name: str
    lat: str
    lon: str
    camera_id: str
    hardware_id: str
    status: str = Field(default="inactive")


# @app.get("/", response_class=HTMLResponse, include_in_schema=False)
# async def main():
#     with open("templates/upload.html") as f:
#         html_content = f.read()
#     return HTMLResponse(content=html_content)

@app.get("/", include_in_schema=False)
async def main():
    return RedirectResponse(url="/ami-data-upload/docs") # docs or /ami-data-upload/docs#

@app.get("/get-deployments/", tags=["Deployments"])
async def get_deployments(username: str = Depends(authenticate)):
    return JSONResponse(content=deployments_info)

def increment_id(data, current_id):
    id_list = []
    for obj in data:
        if obj[current_id] != '':
            id_list.append(obj[current_id])
    # Extract the str part of each ID
    id_type = id_list[0][:3]
    # Extract the numeric part of each ID and convert it to an integer
    numeric_ids = [int(id[3:]) for id in id_list]
    # Find the maximum numeric ID
    max_numeric_id = max(numeric_ids)+1
    # Format back to the original ID format with leading zeros
    highest_id = f"{id_type}{max_numeric_id:06}"
    return highest_id


@app.post("/create-deployment/", tags=["Deployments"])
async def create_deployment(new_deployment: NewDeployment, username: str = Depends(authenticate)):
    try:
        global deployments_info
        # Append the new deployment to the CSV file
        with open('deployments_info.csv', 'a', newline='') as csvfile:
            # TODO: check is location already exists
            location_id = increment_id(deployments_info, 'location_id')
            # TODO: check is camera id already exists
            system_id = increment_id(deployments_info, 'system_id')
            deployment_id = increment_id(deployments_info, 'deployment_id')
            deployment = Deployment(country=new_deployment.country,
                                    country_code=new_deployment.country_code,
                                    location_name=new_deployment.location_name,
                                    lat=new_deployment.lat,
                                    lon=new_deployment.lon,
                                    location_id=location_id,
                                    camera_id=new_deployment.camera_id,
                                    system_id=system_id,
                                    hardware_id=new_deployment.hardware_id,
                                    deployment_id=deployment_id,
                                    status=new_deployment.status)
            fieldnames = ['country', 'country_code', 'location_name', "lat", "lon", "location_id", 'camera_id',
                          "system_id", 'hardware_id', 'deployment_id', 'status']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writerow(deployment.dict())

        # Reload deployments info
        deployments_info = load_deployments_info()

        return JSONResponse(status_code=201, content={"message": "Deployment created successfully"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})


@app.put("/update-deployment/", tags=["Deployments"])
async def update_deployment(deployment: Deployment, username: str = Depends(authenticate)):
    try:
        # Reload deployments info
        global deployments_info

        # Find the deployment to update
        for i, existing_deployment in enumerate(deployments_info):
            if existing_deployment['deployment_id'] == deployment.deployment_id:
                deployments_info[i] = deployment.dict()
                break
            if i == len(deployments_info)-1:
                return JSONResponse(status_code=404, content={"message": "Deployment not found"})

        # Write the updated deployments back to the CSV file
        with open('deployments_info.csv', 'w', newline='') as csvfile:
            fieldnames = ['country', 'country_code', 'location_name', "lat", "lon", "location_id", 'camera_id',
                          "system_id", 'hardware_id', 'deployment_id', 'status']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for dep in deployments_info:
                writer.writerow(dep)

        deployments_info = load_deployments_info()
        return JSONResponse(status_code=200, content={"message": "Deployment updated successfully"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})


@app.get("/list-data/", tags=["Other"])
async def list_data(
        country_location_name: str = Query("", enum=sorted(list(valid_countries_location_names)),
                                           description="Country and location names."),
        data_type: str = Query("", enum=list(valid_data_types), description=""),
        username: str = Depends(authenticate)
):
    country, location_name = country_location_name.split(" - ")
    country_code = [d['country_code'] for d in deployments_info if d['country'] == country][0]
    s3_bucket_name = country_code.lower()
    deployment_id = [d['deployment_id'] for d in deployments_info if d['country'] == country
                     and d['location_name'] == location_name][0]
    prefix = deployment_id + "/" + data_type
    files = []
    try:
        async with session.client('s3',
                                  aws_access_key_id=AWS_ACCESS_KEY_ID,
                                  aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                                  region_name=AWS_REGION,
                                  endpoint_url=AWS_URL_ENDPOINT) as s3_client:
            paginator = s3_client.get_paginator('list_objects_v2')
            operation_parameters = {'Bucket': s3_bucket_name, 'Prefix': prefix}
            async for page in paginator.paginate(**operation_parameters):
                for obj in page.get('Contents', []):
                    files.append(obj['Key'])
            return JSONResponse(status_code=200, content={"files": files})
    except NoCredentialsError:
        return JSONResponse(status_code=403, content={"Credentials not available"})
    except PartialCredentialsError:
        return JSONResponse(status_code=400, content={"Incomplete credentials"})
    except ClientError:
        return JSONResponse(status_code=404, content={"The AWS Access Key Id does not exist in our records"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})

@app.get("/count-data/", tags=["Other"])
async def count_data(
        country_location_name: str = Query("", enum=sorted(list(valid_countries_location_names)),
                                           description="Country and location names."),
        data_type: str = Query("", enum=list(valid_data_types), description=""),
        username: str = Depends(authenticate)
):
    country, location_name = country_location_name.split(" - ")
    country_code = [d['country_code'] for d in deployments_info if d['country'] == country][0]
    s3_bucket_name = country_code.lower()
    deployment_id = [d['deployment_id'] for d in deployments_info if d['country'] == country
                     and d['location_name'] == location_name][0]
    prefix = deployment_id + "/" + data_type
    files = []
    try:
        async with session.client('s3',
                                  aws_access_key_id=AWS_ACCESS_KEY_ID,
                                  aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                                  region_name=AWS_REGION,
                                  endpoint_url=AWS_URL_ENDPOINT) as s3_client:
            paginator = s3_client.get_paginator('list_objects_v2')
            operation_parameters = {'Bucket': s3_bucket_name, 'Prefix': prefix}

            count = 0

            async for page in paginator.paginate(**operation_parameters):

                count += page["KeyCount"]
                # for obj in page.get('Contents', []):
                #     files.append(obj['Key'])
            return JSONResponse(status_code=200, content={"count": count})
    except NoCredentialsError:
        return JSONResponse(status_code=403, content={"Credentials not available"})
    except PartialCredentialsError:
        return JSONResponse(status_code=400, content={"Incomplete credentials"})
    except ClientError:
        return JSONResponse(status_code=404, content={"The AWS Access Key Id does not exist in our records"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})

@app.get("/logs/", tags=["Other"])
async def get_logs(username: str = Depends(authenticate)):
    try:
        with open('upload_logs.log', 'r') as log_file:
            log_content = log_file.read()
        return PlainTextResponse(log_content)
    except Exception as e:
        return JSONResponse(status_code=500, content=f"Error reading log file: {e}")


@app.post("/create-bucket/", tags=["Other"])
async def create_bucket(bucket_name: str = Query("", description="Bucket are named based on countries "
                                                                 "Alpha-3 code, check this link: "
                                                                 "https://www.iban.com/country-codes. "
                                                                 "E.g. The United Kingdom would be gbr"),
                                                                 username: str = Depends(authenticate)):
    async with session.client('s3',
                              aws_access_key_id=AWS_ACCESS_KEY_ID,
                              aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                              region_name=AWS_REGION,
                              endpoint_url=AWS_URL_ENDPOINT) as s3_client:
        try:
            await s3_client.create_bucket(Bucket=bucket_name, CreateBucketConfiguration={'LocationConstraint': AWS_REGION})
            return JSONResponse(status_code=200, content={"message": f"Bucket '{bucket_name}' created successfully"})
        except s3_client.exceptions.BucketAlreadyExists:
            return JSONResponse(status_code=409, content={f"Bucket {bucket_name} already exists."})
        except s3_client.exceptions.BucketAlreadyOwnedByYou:
            return JSONResponse(status_code=409, content={f"Bucket {bucket_name} is already owned by you."})
        except Exception as e:
            return JSONResponse(status_code=500, content={f"Error creating bucket: {str(e)}"})


@app.post("/generate-presigned-url/", tags=["Data"])
async def generate_presigned_url(
    name: str = Form(...),
    country: str = Form(...),
    deployment: str = Form(...),
    data_type: str = Form(...),
    filename: str = Form(...),
    file_type: str = Form(...),
    username: str = Depends(authenticate)
    ):
    bucket_name = country.lower()
    key = f"{deployment}/{data_type}/{filename}"

    s3 = boto3.client('s3',
                      aws_access_key_id=AWS_ACCESS_KEY_ID,
                      aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                      region_name=AWS_REGION,
                      endpoint_url=AWS_URL_ENDPOINT)
    try:
        # Generate a presigned URL for the S3
        presigned_url = s3.generate_presigned_url('put_object',
                                                  Params={"Bucket": bucket_name,
                                                          "Key": key,
                                                          "ContentType": file_type},
                                                  ExpiresIn=3600)  # URL expires in 1 hour

        return JSONResponse(status_code=200, content=presigned_url)
    except NoCredentialsError:
        return JSONResponse(status_code=403, content={"error": "No AWS credentials found"})
    except PartialCredentialsError:
        return JSONResponse(status_code=403, content={"error": "Incomplete AWS credentials"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})  

@app.post("/upload/", tags=["Data"])
async def upload(
        name: str = Form(...),
        country: str = Form(...),
        deployment: str = Form(...),
        data_type: str = Form(...),
        files: List[UploadFile] = File(...),
        username: str = Depends(authenticate)
):
    start_time = perf_counter()
    s3_bucket_name = country.lower()
    key = f"{deployment}/{data_type}"

    try:
        tasks = [upload_file(s3_bucket_name, key, file, name) for file in files]
        await asyncio.gather(*tasks)
    except Exception as e:
        print("Error:", e)
        return JSONResponse(status_code=500, content={str(e)})

    end_time = perf_counter()
    print(f"{end_time - start_time} seconds.")

    logger.info(f"User {name} from {country} uploaded {len(files)} {data_type} to deployment {deployment}.")
    return JSONResponse(status_code=200, content={"message": "All files uploaded and verified successfully"})


async def upload_file(s3_bucket_name, key, file, name):
    async with session.client('s3',
                              aws_access_key_id=AWS_ACCESS_KEY_ID,
                              aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                              region_name=AWS_REGION,
                              endpoint_url=AWS_URL_ENDPOINT) as s3_client:
        try:
            # Upload updated file to S3
            await s3_client.upload_fileobj(file.file, s3_bucket_name, f"{key}/{file.filename}")
            # print(f"File {key}/{file.filename} uploaded successfully.")
        except Exception as e:
            logger.error(f"Error from User {name} when uploading {file.filename} to {s3_bucket_name}/{key}. Error: {e}")
            return JSONResponse(status_code=500, content={"message": f"Error uploading {key}/{file.filename}: {e}"})

@app.post("/check-file-exist/", tags=["Data"])
async def check_file_exist(
    name: str = Form(...),
    country: str = Form(...),
    deployment: str = Form(...),
    data_type: str = Form(...),
    filename: str = Form(...),
    username: str = Depends(authenticate)
):
    bucket_name = country.lower()
    key = f"{deployment}/{data_type}/{filename}"

    s3 = boto3.client('s3',
                      aws_access_key_id=AWS_ACCESS_KEY_ID,
                      aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
                      region_name=AWS_REGION,
                      endpoint_url=AWS_URL_ENDPOINT)

    try:
        s3.head_object(Bucket=bucket_name, Key=key)
        message = {"exists": True}  # File exists
        return JSONResponse(status_code=200, content=message)
    except s3.exceptions.ClientError as e:
        if e.response['Error']['Code'] == '404':
            message = {"exists": False}  # File doesn't exist
            return JSONResponse(status_code=200, content=message)
        return JSONResponse(status_code=500, content={"message": f"{e}"})
    except NoCredentialsError:
        return JSONResponse(status_code=403, content={"message": "No AWS credentials found"})
    except PartialCredentialsError:
        return JSONResponse(status_code=403, content={"message": "Incomplete AWS credentials"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": f"{e}"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
