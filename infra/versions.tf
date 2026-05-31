terraform {
  required_version = ">= 1.8.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"   # T4: updated from ~>5.50
    }
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"   # T5: updated from ~>3.100
    }
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"   # T6: updated from ~>5.30
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.4"
    }
    datadog = {
      source  = "DataDog/datadog"
      version = "~> 3.0"
    }
  }
}
