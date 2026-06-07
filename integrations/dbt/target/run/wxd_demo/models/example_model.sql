create or replace view
    "iceberg_data"."dbt_demo"."example_model__dbt_tmp"
  as
    -- trivial model to validate dbt -> watsonx.data Presto end-to-end
select 1 as id, 'watsonx.data + dbt works' as message
  ;
