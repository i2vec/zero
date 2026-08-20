"""Normalized resource integration failures."""

class ResourceIntegrationError(RuntimeError):
    pass


class RegistryUnavailable(ResourceIntegrationError):
    pass


class RegistryRateLimited(RegistryUnavailable):
    pass


class RegistryBusinessError(ResourceIntegrationError):
    pass


class RegistryValidationError(ResourceIntegrationError):
    pass


class RegistryIndexError(ResourceIntegrationError):
    pass


class RegistryConflict(ResourceIntegrationError):
    pass


class ArtifactUnavailable(ResourceIntegrationError):
    pass


class ArtifactMutable(ResourceIntegrationError):
    pass


class DeployMasterBuildFailed(ResourceIntegrationError):
    pass


class DeployMasterVerificationFailed(ResourceIntegrationError):
    pass
