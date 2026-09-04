#!/usr/bin/env bash
set -x

#S3
aws --endpoint-url=http://localhost:4566 s3 mb s3://call-recordings --region us-east-1