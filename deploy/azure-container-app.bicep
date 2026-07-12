// sqldoc agent on Azure Container Apps, with persistent Azure Files storage for
// the agent store (/data) and env-var configuration for credentials.
//
//   az deployment group create -g <rg> -f deploy/azure-container-app.bicep \
//     -p image='myregistry.azurecr.io/sqldoc:2.7.0' \
//        anthropicApiKey='***'
//
// The .sqldoc.yml config is passed as a base64 secret and written to
// /app/.sqldoc.yml by the container's start command.

@description('Container image (registry/repo:tag) for sqldoc.')
param image string

@description('Location for all resources.')
param location string = resourceGroup().location

@description('Name prefix.')
param name string = 'sqldoc'

@description('Base64-encoded .sqldoc.yml config.')
@secure()
param configBase64 string = ''

@description('Optional Anthropic API key for cloud AI.')
@secure()
param anthropicApiKey string = ''

var storageShareName = 'sqldoc-data'

resource logs 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: '${name}-logs'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: toLower('${name}stg${uniqueString(resourceGroup().id)}')
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
}

resource fileService 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  name: 'default'
  parent: storage
}

resource share 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  name: storageShareName
  parent: fileService
  properties: { shareQuota: 5 }
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: '${name}-env'
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
  }
}

resource envStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  name: storageShareName
  parent: env
  properties: {
    azureFile: {
      accountName: storage.name
      accountKey: storage.listKeys().keys[0].value
      shareName: storageShareName
      accessMode: 'ReadWrite'
    }
  }
}

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: name
  location: location
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8080
        transport: 'http'
      }
      secrets: concat(
        [ { name: 'config-b64', value: configBase64 } ],
        empty(anthropicApiKey) ? [] : [ { name: 'anthropic-key', value: anthropicApiKey } ]
      )
    }
    template: {
      containers: [
        {
          name: name
          image: image
          command: [ '/bin/sh', '-c' ]
          args: [ 'echo "$CONFIG_B64" | base64 -d > /app/.sqldoc.yml && exec sqldoc agent start --foreground' ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: concat(
            [
              { name: 'SQLDOC_AGENT_HOME', value: '/data' }
              { name: 'CONFIG_B64', secretRef: 'config-b64' }
            ],
            empty(anthropicApiKey) ? [] : [ { name: 'ANTHROPIC_API_KEY', secretRef: 'anthropic-key' } ]
          )
          volumeMounts: [ { volumeName: 'data', mountPath: '/data' } ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
      volumes: [
        { name: 'data', storageType: 'AzureFile', storageName: storageShareName }
      ]
    }
  }
}

output dashboardUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
