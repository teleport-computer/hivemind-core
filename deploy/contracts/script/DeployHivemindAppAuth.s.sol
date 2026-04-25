// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import "forge-std/Script.sol";
import "../src/HivemindAppAuth.sol";

/// Deploy HivemindAppAuth. Set HIVEMIND_APP_AUTH_OWNER to the EOA that
/// should own the contract. If unset, uses the broadcaster itself (fine
/// for testnet).
///
/// Usage:
///   forge script script/DeployHivemindAppAuth.s.sol \
///     --rpc-url $ETH_SEPOLIA_RPC_URL --broadcast
contract DeployHivemindAppAuth is Script {
    function run() external {
        uint256 deployerKey = vm.envUint("PRIVATE_KEY");
        address owner = vm.envOr("HIVEMIND_APP_AUTH_OWNER", vm.addr(deployerKey));

        vm.startBroadcast(deployerKey);
        HivemindAppAuth auth = new HivemindAppAuth(owner);
        vm.stopBroadcast();

        console2.log("HivemindAppAuth deployed at:", address(auth));
        console2.log("Owner:", owner);
    }
}
