#!/bin/zsh
cd "$(dirname "$0")/../.." || exit 1   # repo root
CPD=$(cat tmp/.cpdurl); IID="${WXD_INSTANCE_ID:?set WXD_INSTANCE_ID}"; TOKEN=$(cat tmp/.cpdtoken); AK=$(cat tmp/.s3ak); SK=$(cat tmp/.s3sk); KEY=$(cat tmp/.wxdapikey)
EP='http://ibm-lh-lakehouse-minio-svc.cpd.svc.cluster.local:9000'
BODY=$(python3 -c "
import json
def b(bkt):
  return {f'spark.hadoop.fs.s3a.bucket.{bkt}.endpoint':'$EP',f'spark.hadoop.fs.s3a.bucket.{bkt}.access.key':'$AK',f'spark.hadoop.fs.s3a.bucket.{bkt}.secret.key':'$SK',f'spark.hadoop.fs.s3a.bucket.{bkt}.path.style.access':'true',f'spark.hadoop.fs.s3a.bucket.{bkt}.aws.credentials.provider':'org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider',f'spark.hadoop.fs.s3a.bucket.{bkt}.connection.ssl.enabled':'false',f'spark.hadoop.fs.s3a.bucket.{bkt}.s3.signing-algorithm':'',f'spark.hadoop.fs.s3a.bucket.{bkt}.signing-algorithm':'',f'spark.hadoop.fs.s3a.bucket.{bkt}.custom.signers':''}
conf={'spark.app.name':'ice-final'}
conf.update(b('spark-apps')); conf.update(b('iceberg-bucket'))
conf['spark.hive.metastore.client.plain.username']='ibmlhapikey_cpadmin'
conf['spark.hive.metastore.client.plain.password']='$KEY'
print(json.dumps({'application_details':{'application':'s3a://spark-apps/iceberg_demo.py','spark_version':'3.5','conf':conf}}))")
APPID=$(curl -sk -X POST "$CPD/lakehouse/api/v3/$IID/spark_engines/spark6/applications" -H "Authorization: Bearer $TOKEN" -H "LhInstanceId: $IID" -H "Content-Type: application/json" -d "$BODY" 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "APP_ID=$APPID"; S8="${APPID:0:8}"; SEEN=0; : > tmp/ice.log
for i in $(seq 1 80); do
  MP=$(oc get pods -n cpd --no-headers 2>/dev/null | grep "spark-master-deployment-$S8" | awk '{print $1}' | head -1)
  if [ -n "$MP" ]; then SEEN=1; L=$(oc logs -n cpd "$MP" -c spark-master --tail=-1 2>/dev/null); [ -n "$L" ] && echo "$L" > tmp/ice.log
  else [ $SEEN -eq 1 ] && { echo POD_GONE; break; }; fi
  sleep 5
done
echo "FINAL=$(curl -sk "$CPD/lakehouse/api/v3/$IID/spark_engines/spark6/applications/$APPID" -H "Authorization: Bearer $TOKEN" -H "LhInstanceId: $IID" 2>/dev/null | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('state'),d.get('return_code'))")"; echo DONE
