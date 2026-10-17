terraform {
  # Storage is bootstrapped separately; supply noncredential settings at init.
  backend "azurerm" {}
}
