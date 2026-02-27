----- kicking off the repo ---
I want to start using nautilus trader for running backtest, and in later stage for live trading. For now there's a few things we can work on to get started. Note that none of the following step should involve amending the existing code of the nautilius_trader framework. Keep our changes concise, elegant, readable. Avoid emojis.

1. Please help me properly setup uv environment under this repo
2. Go through the codebase to have a understanding about how the framework works and put together an initial AGENTS.md file for future references. Key things to watch is 
    * How to proper subscribe, request data
    * Typical structure of setting up strategy
    * How are order matching simulated upon different data and order type, especially on its timeliness (fill on same bar or next bar), price (fill on close? If right on limit does it fill? Is it configurable?), partial fill simulation (If order size is larger than available volume or volume is missing, how does it handle it)
3. Check if the repo has a unified data interface, if not, make up a datasource interface so that we can easily add datafeed later. We can start with one example
    * Check out the files in ~/codebase/ocelot/data/raw_taobao, those are China daily level data. We can process and transform those into Bar data for backtesting. For stock data specifically we need to be careful with the adjust factor (复权因子), whenever adjust factor is present, the actual price and volume data we pass into Bar data constructor should always be post adjusted. For this particular source, it should be price_after_adj=price*adj_factor and volume_after_adj = volume/adj_factor.
    * The datasource should always make sure there's a way that the user can easily and efficiently, upon request, access the raw data before processing into nautilus_trader datatypes.
4. Once (3) is done, start off by making an example strategy that subscribes to daily ohlc data from (3) and rebalance daily. Note that it should not rebalance every time it receives a bar, but rather at the end of each day, based on a randomly generated signal for all the stocks available on that day, generate a order list spanning all the stock. As a starter, all those processes can be done using random signal and random mapping to orders, with the key parameters controlled by a .toml config.
5. Create a main program that's used to run backtest of (4), which takes in a .toml config that points to the corresponding strategy as well as the configs needed (random seed etc)

Let's start with these.

-----------
Good, can you now try to profile the performance of this script by testing a grid of different size of stocks and length of backtest ran, to see how efficiently our backtest run given different stock size and time size? Please profile where most of the time is spent so that we have better idea on what to improve later.


------
Good, so seems all the blocker are just how we ourselves load the data source and how the datasource itself is structured. I'm not too worried by that. Let's mimic a more realistic case where the data comes from a database. We may first restructure the china daily ohlcv data into a duckdb, stored somewhere where our directory has visibility to, then change the data source from reading directly from the duckdb . I think it would be much more efficient that way, let me know if you don't agree or have a better way. If we agree, go ahead to implement this and re-run the profiling.


---- 
Excellent. Let's commit what we have so far to make a checkpoint, and then proceed to a few other structure question

1. Is the way we call on_bar still worth optimizing? Given that we still loop through the data without actually having to do anything except when it hits certain condition. Is there a way we can do better utilizing Nautilus trader's framework?
2. Imagine that in real life, there's probably going to be three part of problem
    * A signal calculator that generates the signal, they are like alpha analyts
    * A "trader" or "optimizer" that given our current positions, market condition, risk control, translate them into orders
Given this typical structure, is there a better way we can utilize the Nautilius trader's framework, e.g. the Strategy and Actor class, in order to come up with a better design that's efficient both in real life and backtest? If they all need to subscribe to data for example, does it massively increase the time spent on loading data?

Please investigate above 2 questions by running tests, trying out structure etc. 

--- 
Thank you. I think it's a solid recommendation. Let's break down our example in to the SignalActor and ExecutionStrategy, and execution strategy should subscribe to msgbus looking for signalActor's rebalance signal. We can also include a RiskActor in order to monitor the positions pnl, making it possible to submit profit taking or stop loss actions.

Once the above modification is done, let's commit it to the xip branch, and I need your help with one framework change:
The OHLC Bar type available in Nautilius trader is insufficient, we need to extend it to have open,high,low,close,vwap,volume,adj_factor. vwap and adj_factor should both be float, vwap if missing, then default value would be the average of high,low,close; if adj_factor value is missing, then default to 1.0. Note that we should by default regard that price*adj_factor, volume/adj_factor should be able to recover the data to as-of-time. Also, is volume possible to be a decimal or float? It's best that it should be made possible.

Please addres the two issues described above.