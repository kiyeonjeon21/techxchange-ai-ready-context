"""watsonx.data Spark -> Iceberg (iceberg_data catalog). Minimal: no timestamp."""
from pyspark.sql import SparkSession
spark = SparkSession.builder.appName("iceberg-demo").getOrCreate()
spark.sql("CREATE SCHEMA IF NOT EXISTS iceberg_data.demo_db")
spark.sql("DROP TABLE IF EXISTS iceberg_data.demo_db.spark_iceberg_test")
spark.sql("CREATE TABLE iceberg_data.demo_db.spark_iceberg_test (id INT, msg STRING) USING iceberg")
spark.sql("INSERT INTO iceberg_data.demo_db.spark_iceberg_test VALUES (1,'created by spark'),(2,'lakehouse works'),(3,'spark to presto')")
print("=== contents ===")
spark.sql("SELECT * FROM iceberg_data.demo_db.spark_iceberg_test ORDER BY id").show(truncate=False)
print("ROW_COUNT=" + str(spark.sql("SELECT count(*) c FROM iceberg_data.demo_db.spark_iceberg_test").collect()[0]["c"]))
spark.stop()
