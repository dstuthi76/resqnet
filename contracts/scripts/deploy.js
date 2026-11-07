// Deploys ResQNetRelief, creates/funds 3 NGO wallets, grants roles, funds the pool.
//
//   npx hardhat run scripts/deploy.js --network sepolia
//
// Reads from ../.env:  ORACLE_PRIVATE_KEY, SEPOLIA_RPC_URL,
//   optional NGO_PRIVATE_KEYS (comma separated), POOL_FUND_ETH (default 0.03),
//   NGO_GAS_ETH (default 0.005), PAYOUT_WEI, HOURS_THRESHOLD, CONFIRMATIONS_REQUIRED
const { ethers, network } = require("hardhat");

async function main() {
  const [deployer] = await ethers.getSigners();
  const env = process.env;
  const payout = BigInt(env.PAYOUT_WEI || ethers.parseEther("0.001").toString());
  const hours = Number(env.HOURS_THRESHOLD || 48);
  const confs = Number(env.CONFIRMATIONS_REQUIRED || 2);
  const poolEth = env.POOL_FUND_ETH || "0.03";
  const ngoGasEth = env.NGO_GAS_ETH || "0.005";

  console.log(`Network: ${network.name}`);
  console.log(`Deployer / oracle: ${deployer.address}`);
  console.log(`Balance: ${ethers.formatEther(await ethers.provider.getBalance(deployer.address))} ETH`);

  const F = await ethers.getContractFactory("ResQNetRelief");
  const c = await F.deploy(payout, hours, confs);
  await c.waitForDeployment();
  const addr = await c.getAddress();
  console.log(`ResQNetRelief deployed at ${addr}`);
  const waiting = Number(env.WAITING_PERIOD_S || 0);
  if (waiting > 0) {
    await (await c.setRule(payout, hours, confs, 50, waiting)).wait();
    console.log(`Waiting period set to ${waiting} s`);
  }

  let ngoKeys = (env.NGO_PRIVATE_KEYS || "").split(",").map((s) => s.trim()).filter(Boolean);
  let generated = false;
  if (ngoKeys.length === 0) {
    ngoKeys = [0, 1, 2].map(() => ethers.Wallet.createRandom().privateKey);
    generated = true;
  }
  const NGO_ROLE = await c.NGO_ROLE();
  for (const k of ngoKeys) {
    const w = new ethers.Wallet(k);
    await (await c.grantRole(NGO_ROLE, w.address)).wait();
    await (await deployer.sendTransaction({ to: w.address, value: ethers.parseEther(ngoGasEth) })).wait();
    console.log(`NGO wallet ${w.address}: role granted, funded ${ngoGasEth} ETH for gas`);
  }

  await (await c.fundPool({ value: ethers.parseEther(poolEth) })).wait();
  console.log(`Relief pool funded with ${poolEth} ETH`);

  console.log("\n==== add these lines to your .env ====");
  console.log(`CHAIN_MODE=sepolia`);
  console.log(`CONTRACT_ADDRESS=${addr}`);
  if (generated) console.log(`NGO_PRIVATE_KEYS=${ngoKeys.join(",")}`);
  console.log("======================================");
  if (network.name === "sepolia") {
    console.log(`Explorer: https://sepolia.etherscan.io/address/${addr}`);
    console.log(`Verify (optional): npx hardhat verify --network sepolia ${addr} ${payout} ${hours} ${confs}`);
  }
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
