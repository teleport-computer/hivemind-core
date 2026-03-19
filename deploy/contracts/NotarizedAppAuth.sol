// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.22;

/// @title NotarizedAppAuth — deploy governance for hivemind-core
/// @notice Extends Phala's IAppAuth with a notarized deploy flow:
///   1. Owner calls requestDeploy(hash) → emits event
///   2. Monitoring TEE sees event, logs it, calls notarize(hash, logCID)
///   3. KMS calls isAppAllowed() → checks if composeHash was notarized
///
/// The notary address is derived inside the monitoring TEE via
///   getKey("/notary/signer") → deterministic, bound to monitoring TEE's code.
///   Developer cannot produce this key without running the exact monitoring code.

interface IAppAuth {
    struct AppBootInfo {
        address appId;
        bytes32 composeHash;
        address instanceId;
        bytes32 deviceId;
        bytes32 mrAggregated;
        bytes32 mrSystem;
        bytes32 osImageHash;
        string tcbStatus;
        string[] advisoryIds;
    }

    function isAppAllowed(
        AppBootInfo calldata bootInfo
    ) external view returns (bool isAllowed, string memory reason);
}

contract NotarizedAppAuth is IAppAuth {
    address public owner;
    address public notary;

    mapping(bytes32 => bool) public allowedHashes;

    event DeployRequested(bytes32 indexed composeHash, uint256 timestamp);
    event DeployNotarized(bytes32 indexed composeHash, bytes logCID, uint256 timestamp);
    event ComposeHashRevoked(bytes32 indexed composeHash);
    event NotaryUpdated(address indexed oldNotary, address indexed newNotary);

    modifier onlyOwner() {
        require(msg.sender == owner, "not owner");
        _;
    }

    modifier onlyNotary() {
        require(msg.sender == notary, "not notary");
        _;
    }

    constructor(address _notary) {
        owner = msg.sender;
        notary = _notary;
    }

    /// @notice Owner requests a new deploy. Monitoring TEE watches for this event.
    function requestDeploy(bytes32 composeHash) external onlyOwner {
        emit DeployRequested(composeHash, block.timestamp);
    }

    /// @notice Monitoring TEE notarizes a deploy after logging it.
    /// @param composeHash The docker-compose hash to approve
    /// @param logCID IPFS CID of the transparency log entry
    function notarize(bytes32 composeHash, bytes calldata logCID) external onlyNotary {
        allowedHashes[composeHash] = true;
        emit DeployNotarized(composeHash, logCID, block.timestamp);
    }

    /// @notice Owner revokes a previously approved hash.
    function revoke(bytes32 composeHash) external onlyOwner {
        allowedHashes[composeHash] = false;
        emit ComposeHashRevoked(composeHash);
    }

    /// @notice Owner updates the notary address (e.g. after monitoring TEE redeployment).
    function setNotary(address _notary) external onlyOwner {
        emit NotaryUpdated(notary, _notary);
        notary = _notary;
    }

    /// @notice Called by Phala KMS via DstackKms to check if a CVM is allowed to boot.
    function isAppAllowed(
        AppBootInfo calldata bootInfo
    ) external view override returns (bool, string memory) {
        if (!allowedHashes[bootInfo.composeHash]) {
            return (false, "Compose hash not notarized");
        }
        return (true, "");
    }

    /// @notice ERC-165 support for IAppAuth interface detection.
    function supportsInterface(bytes4 interfaceId) external pure returns (bool) {
        return interfaceId == 0x1e079198  // IAppAuth
            || interfaceId == 0x01ffc9a7; // ERC165
    }
}
