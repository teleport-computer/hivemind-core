// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title HivemindAppAuth
/// @notice On-chain whitelist of authorized compose_hash values for the
/// Hivemind TDX enclave. Read by the Hivemind CLI during `_require_trust`
/// to auto-accept approved compose hashes (no y/N prompt) and hard-abort
/// revoked ones. Also readable by dstack KMS / any auditor.
///
/// Pattern ported verbatim from feedling-mcp-v1's FeedlingAppAuth.sol —
/// the third of feedling's three attestation bindings. See
/// ~/.claude/projects/.../memory/project_attestation_binding.md.
///
/// v1: single-EOA owner, no timelock, no multisig. Upgrade paths are
/// deliberately left open.
contract HivemindAppAuth {
    // ----------------------------------------------------------------
    // Types
    // ----------------------------------------------------------------

    struct ReleaseEntry {
        bool approved;
        uint64 approvedAt;
        uint64 revokedAt;
        string gitCommit;
        string composeYamlURI;
    }

    // ----------------------------------------------------------------
    // Storage
    // ----------------------------------------------------------------

    address public owner;
    mapping(bytes32 => ReleaseEntry) public releases;
    bytes32[] public releaseOrder;

    // ----------------------------------------------------------------
    // Events
    // ----------------------------------------------------------------

    event ComposeHashAdded(
        bytes32 indexed composeHash,
        string gitCommit,
        string composeYamlURI,
        uint64 approvedAt
    );

    event ComposeHashRevoked(
        bytes32 indexed composeHash,
        uint64 revokedAt
    );

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    // ----------------------------------------------------------------
    // Errors
    // ----------------------------------------------------------------

    error NotOwner();
    error AlreadyApproved(bytes32 composeHash);
    error NotApproved(bytes32 composeHash);
    error ZeroHash();
    error ZeroAddress();
    error EmptyString();

    // ----------------------------------------------------------------
    // Construction
    // ----------------------------------------------------------------

    constructor(address _owner) {
        if (_owner == address(0)) revert ZeroAddress();
        owner = _owner;
        emit OwnershipTransferred(address(0), _owner);
    }

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ----------------------------------------------------------------
    // Write path (owner only)
    // ----------------------------------------------------------------

    function addComposeHash(
        bytes32 composeHash,
        string calldata gitCommit,
        string calldata composeYamlURI
    ) external onlyOwner {
        if (composeHash == bytes32(0)) revert ZeroHash();
        if (bytes(gitCommit).length == 0) revert EmptyString();
        if (bytes(composeYamlURI).length == 0) revert EmptyString();

        ReleaseEntry storage entry = releases[composeHash];
        if (entry.approved) revert AlreadyApproved(composeHash);

        entry.approved = true;
        entry.approvedAt = uint64(block.timestamp);
        entry.revokedAt = 0;
        entry.gitCommit = gitCommit;
        entry.composeYamlURI = composeYamlURI;

        if (!_inOrderList(composeHash)) {
            releaseOrder.push(composeHash);
        }

        emit ComposeHashAdded(composeHash, gitCommit, composeYamlURI, entry.approvedAt);
    }

    function revoke(bytes32 composeHash) external onlyOwner {
        ReleaseEntry storage entry = releases[composeHash];
        if (!entry.approved) revert NotApproved(composeHash);
        entry.approved = false;
        entry.revokedAt = uint64(block.timestamp);
        emit ComposeHashRevoked(composeHash, entry.revokedAt);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        address prev = owner;
        owner = newOwner;
        emit OwnershipTransferred(prev, newOwner);
    }

    // ----------------------------------------------------------------
    // Read path
    // ----------------------------------------------------------------

    function isAppAllowed(bytes32 composeHash) external view returns (bool) {
        return releases[composeHash].approved;
    }

    function releaseCount() external view returns (uint256) {
        return releaseOrder.length;
    }

    function getRelease(uint256 index) external view returns (
        bytes32 composeHash,
        bool approved,
        uint64 approvedAt,
        uint64 revokedAt,
        string memory gitCommit,
        string memory composeYamlURI
    ) {
        composeHash = releaseOrder[index];
        ReleaseEntry storage e = releases[composeHash];
        return (composeHash, e.approved, e.approvedAt, e.revokedAt, e.gitCommit, e.composeYamlURI);
    }

    // ----------------------------------------------------------------
    // Internals
    // ----------------------------------------------------------------

    function _inOrderList(bytes32 composeHash) internal view returns (bool) {
        uint256 len = releaseOrder.length;
        for (uint256 i = 0; i < len; i++) {
            if (releaseOrder[i] == composeHash) return true;
        }
        return false;
    }
}
