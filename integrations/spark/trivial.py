from pyspark.sql import SparkSession
spark = SparkSession.builder.appName("trivial").getOrCreate()
n = spark.range(0, 1000).count()
print("TRIVIAL_RESULT_COUNT=" + str(n))
spark.stop()
