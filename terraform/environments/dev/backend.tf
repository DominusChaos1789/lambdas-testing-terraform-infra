# Configure remote state before running in a shared/team context.
# Create the bucket + DynamoDB lock table once, then uncomment.
#
# terraform {
#   backend "s3" {
#     bucket         = "nexa-terraform-state-dev"
#     key            = "empatia/transcription-forwarder/dev.tfstate"
#     region         = "us-east-1"
#     dynamodb_table = "nexa-terraform-locks"
#     encrypt        = true
#   }
# }
