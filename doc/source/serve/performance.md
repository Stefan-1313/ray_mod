# Performance Tuning

This section should help you:

- understand Ray Serve's performance characteristics
- find ways to debug and tune your Serve application's performance

:::{note}
This section offers some tips and tricks to improve your Ray Serve application's performance. Check out the [architecture page](serve-architecture) for helpful context, including an overview of the HTTP proxy actor and deployment replica actors.
:::

```{contents}
```

## Performance and known benchmarks

We are continuously benchmarking Ray Serve. The metrics we care about are latency, throughput, and scalability. We can confidently say:

- Ray Serve’s latency overhead is single digit milliseconds, around 1-2 milliseconds on average.
- For throughput, Serve achieves about 3-4k queries per second on a single machine (8 cores) using 1 HTTP proxy actor and 8 replicas performing no-op requests.
- It is horizontally scalable so you can add more machines to increase the overall throughput. Ray Serve is built on top of Ray,
  so its scalability is bounded by Ray’s scalability. Please see Ray’s [scalability envelope](https://github.com/ray-project/ray/blob/master/release/benchmarks/README.md)
  to learn more about the maximum number of nodes and other limitations.

We run long-running benchmarks nightly:

```{eval-rst}
.. list-table::
   :header-rows: 1

   * - Benchmark
     - Description
     - Cluster Details
     - Performance Numbers
   * - `Single Deployment <https://github.com/ray-project/ray/blob/de227ac407d6cad52a4ead09571eff6b1da73a6d/release/serve_tests/workloads/single_deployment_1k_noop_replica.py>`_
     - Runs 10 minute `wrk <https://github.com/wg/wrk>`_ trial on a single no-op deployment with 1000 replicas.
     - Head node: AWS EC2 m5.8xlarge. 32 worker nodes: AWS EC2 m5.8xlarge.
     - * per_thread_latency_avg_ms = 22.41
       * per_thread_latency_max_ms = 1400.0
       * per_thread_avg_tps = 55.75
       * per_thread_max_tps = 121.0
       * per_node_avg_tps = 553.17
       * per_node_avg_transfer_per_sec_KB = 83.19
       * cluster_total_thoughput = 10954456
       * cluster_total_transfer_KB = 1647441.9199999997
       * cluster_total_timeout_requests = 0
       * cluster_max_P50_latency_ms = 8.84
       * cluster_max_P75_latency_ms = 35.31
       * cluster_max_P90_latency_ms = 49.69
       * cluster_max_P99_latency_ms = 56.5
   * - `Multiple Deployments <https://github.com/ray-project/ray/blob/de227ac407d6cad52a4ead09571eff6b1da73a6d/release/serve_tests/workloads/multi_deployment_1k_noop_replica.py>`_
     - Runs 10 minute `wrk <https://github.com/wg/wrk>`_ trial on 10 deployments with 100 replicas each. Each deployment recursively sends queries to up to 5 other deployments.
     - Head node: AWS EC2 m5.8xlarge. 32 worker nodes: AWS EC2 m5.8xlarge.
     - * per_thread_latency_avg_ms = 0.0
       * per_thread_latency_max_ms = 0.0
       * per_thread_avg_tps = 0.0
       * per_thread_max_tps = 0.0
       * per_node_avg_tps = 0.35
       * per_node_avg_transfer_per_sec_KB = 0.05
       * cluster_total_thoughput = 6964
       * cluster_total_transfer_KB = 1047.28
       * cluster_total_timeout_requests = 6964.0
       * cluster_max_P50_latency_ms = 0.0
       * cluster_max_P75_latency_ms = 0.0
       * cluster_max_P90_latency_ms = 0.0
       * cluster_max_P99_latency_ms = 0.0
   * - `Deployment Graph: Ensemble <https://github.com/ray-project/ray/blob/f6735f90c72581baf83a9cea7cbbe3ea2f6a56d8/release/serve_tests/workloads/deployment_graph_wide_ensemble.py>`_
     - Runs 10 node ensemble, constructed with a call graph, that performs basic arithmetic at each node. Ensemble pattern routes the input to 10 different nodes, and their outputs are combined to produce the final output. Simulates 4 clients making 20 requests each.
     - Head node: AWS EC2 m5.8xlarge. 0 Worker nodes.
     - * throughput_mean_tps = 8.75
       * throughput_std_tps = 0.43
       * latency_mean_ms = 126.15
       * latency_std_ms = 18.35
```

:::{note}
The performance numbers above come from a recent run of the nightly benchmarks.
:::

<!--- See https://github.com/ray-project/ray/pull/27711 for more context on the benchmarks. -->

Check out [our benchmark workloads'](https://github.com/ray-project/ray/tree/f6735f90c72581baf83a9cea7cbbe3ea2f6a56d8/release/serve_tests/workloads) source code directly to get a better sense of what they test. You can see which cluster templates each benchmark uses [here](https://github.com/ray-project/ray/blob/8eca6ae852e2d23bcf49680fef6f0384a1b63564/release/release_tests.yaml#L2328-L2576) (under the `cluster_compute` key), and you can see what type of nodes each template spins up [here](https://github.com/ray-project/ray/tree/8beb887bbed31ecea3d2813b61833b81c45712e1/release/serve_tests).

You can check out our [microbenchmark instructions](https://github.com/ray-project/ray/blob/master/python/ray/serve/benchmarks/README.md)
to benchmark Ray Serve on your hardware.

## Debugging performance issues

The performance issue you're most likely to encounter is high latency and/or low throughput for requests.

Once you set up [monitoring](serve-monitoring) with Ray and Ray Serve, these issues may appear as:

* `serve_num_router_requests` staying constant while your load increases
* `serve_deployment_processing_latency_ms` spiking up as queries queue up in the background

There are handful of ways to address these issues:

1. Make sure you are using the right hardware and resources:
   * Are you reserving GPUs for your deployment replicas using `ray_actor_options` (e.g. `ray_actor_options={“num_gpus”: 1}`)?
   * Are you reserving one or more cores for your deployment replicas using `ray_actor_options` (e.g. `ray_actor_options={“num_cpus”: 2}`)?
   * Are you setting [OMP_NUM_THREADS](serve-omp-num-threads) to increase the performance of your deep learning framework?
2. Consider using `async` methods in your callable. See [the section below](serve-performance-async-methods).
3. Consider batching your requests. See [the section below](serve-performance-batching-requests).

(serve-performance-async-methods)=
### Using `async` methods

Are you using `async def` in your callable? If you are using `asyncio` and
hitting the same queuing issue mentioned above, you might want to increase
`max_concurrent_queries`. Serve sets a low number (100) by default so the client gets
proper backpressure. You can increase the value in the deployment decorator; e.g.
`@serve.deployment(max_concurrent_queries=1000)`.

(serve-performance-batching-requests)=
### Batching requests

If your deployment can process batches at a sublinear latency
(meaning, for example, that it takes say 1ms to process 1 query and 5ms to process 10 of them)
then batching is your best approach. Check out the [batching guide](serve-batching) and
refactor your deployment to accept batches (especially for GPU-based ML inference). You might want to tune `max_batch_size` and `batch_wait_timeout` in the `@serve.batch` decorator to maximize the benefits:

- `max_batch_size` specifies how big the batch should be. Generally,
  we recommend choosing the largest batch size your function can handle
  without losing the sublinear performance improvement.
  For example, suppose it takes 1ms to process 1 query, 5ms to process 10 queries,
  and 6ms to process 11 queries. Here you should set the batch size to 10
  because adding more queries won’t improve the performance.
- `batch_wait_timeout` specifies the maximum amount of time to wait before
  a batch should be processed, even if it’s not full.  It should be set according
  to the equation:
  
  ```
  batch_wait_timeout + full batch processing time ~= expected latency
  ```

  The larger that `batch_wait_timeout` is, the more full the typical batch will be.
  To maximize throughput, you should set `batch_wait_timeout` as large as possible without exceeding your desired expected latency in the equation above.
