output "webhook_url" {
  description = "POST inbound email payloads here."
  value       = "${aws_apigatewayv2_api.main.api_endpoint}/webhook"
}

output "feedback_url" {
  description = "POST human corrections here."
  value       = "${aws_apigatewayv2_api.main.api_endpoint}/feedback"
}

output "datastore_name" {
  description = "DynamoDB classifications table name."
  value       = aws_dynamodb_table.classifications.name
}

output "classifier_lambda_arn" {
  value = aws_lambda_function.classifier.arn
}

output "event_bus_name" {
  value = aws_cloudwatch_event_bus.main.name
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.url
}
