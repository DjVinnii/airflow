 .. Licensed to the Apache Software Foundation (ASF) under one
    or more contributor license agreements.  See the NOTICE file
    distributed with this work for additional information
    regarding copyright ownership.  The ASF licenses this file
    to you under the Apache License, Version 2.0 (the
    "License"); you may not use this file except in compliance
    with the License.  You may obtain a copy of the License at

 ..   http://www.apache.org/licenses/LICENSE-2.0

 .. Unless required by applicable law or agreed to in writing,
    software distributed under the License is distributed on an
    "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
    KIND, either express or implied.  See the License for the
    specific language governing permissions and limitations
    under the License.



Lineage
========

.. note:: Lineage support is very experimental and subject to change.

Airflow can help track origins of data, what happens to it and where it moves over time. This can aid having
audit trails and data governance, but also debugging of data flows.

Airflow tracks data by means of inlets and outlets of the tasks. Let's work from an example and see how it
works.

.. code-block:: python

    import datetime
    import pendulum

    from airflow.lineage import AUTO
    from airflow.models import DAG
    from airflow.providers.common.compat.lineage.entities import File
    from airflow.providers.standard.operators.bash import BashOperator
    from airflow.providers.standard.operators.empty import EmptyOperator

    FILE_CATEGORIES = ["CAT1", "CAT2", "CAT3"]

    dag = DAG(
        dag_id="example_lineage",
        start_date=pendulum.datetime(2021, 1, 1, tz="UTC"),
        schedule="0 0 * * *",
        catchup=False,
        dagrun_timeout=datetime.timedelta(minutes=60),
    )

    f_final = File(url="/tmp/final")
    run_this_last = EmptyOperator(task_id="run_this_last", dag=dag, inlets=AUTO, outlets=f_final)

    f_in = File(url="/tmp/whole_directory/")
    outlets = []
    for file in FILE_CATEGORIES:
        f_out = File(url="/tmp/{}/{{{{ data_interval_start }}}}".format(file))
        outlets.append(f_out)

    run_this = BashOperator(task_id="run_me_first", bash_command="echo 1", dag=dag, inlets=f_in, outlets=outlets)
    run_this.set_downstream(run_this_last)

Inlets can be a (list of) upstream task ids or statically defined as an attr annotated object
as is, for example, the ``File`` object. Outlets can only be attr annotated object. Both are rendered
at run time. However, the outlets of a task in case they are inlets to another task will not be re-rendered
for the downstream task.

.. note:: Operators can add inlets and outlets automatically if the operator supports it.

In the example DAG task ``run_this`` (``task_id=run_me_first``) is a BashOperator that takes 3 inlets: ``CAT1``, ``CAT2``, ``CAT3``, that are
generated from a list. Note that ``data_interval_start`` is a templated field and will be rendered when the task is running.

.. note:: Behind the scenes Airflow prepares the lineage metadata as part of the ``pre_execute`` method of a task. When the task
          has finished execution ``post_execute`` is called and lineage metadata is pushed into XCOM. Thus if you are creating
          your own operators that override this method make sure to decorate your method with ``prepare_lineage`` and ``apply_lineage``
          respectively.

Shorthand notation
------------------

Shorthand notation is available as well, this works almost equal to unix command line pipes, inputs and outputs.
Note that operator precedence_ still applies. Also the ``|`` operator will only work when the left hand side either
has outlets defined (e.g. by using ``add_outlets(..)`` or has out of the box support of lineage ``operator.supports_lineage == True``.

.. code-block:: python

    f_in > run_this | (run_this_last > outlets)

.. _precedence: https://docs.python.org/3/reference/expressions.html

Hook Lineage
------------

Airflow provides a powerful feature for tracking data lineage not only between tasks but also from hooks used within those tasks.
This functionality helps you understand how data flows throughout your Airflow pipelines.

A global instance of ``HookLineageCollector`` serves as the central hub for collecting lineage information.
Hooks can send details about assets they interact with to this collector.
The collector then uses this data to construct AIP-60 compliant Assets, a standard format for describing assets.

.. code-block:: python

    from airflow.lineage.hook.lineage import get_hook_lineage_collector


    class CustomHook(BaseHook):
        def run(self):
            # run actual code
            collector = get_hook_lineage_collector()
            collector.add_input_asset(self, asset_kwargs={"scheme": "file", "path": "/tmp/in"})
            collector.add_output_asset(self, asset_kwargs={"scheme": "file", "path": "/tmp/out"})

Lineage data collected by the ``HookLineageCollector`` can be accessed using an instance of ``HookLineageReader``,
which is registered in an Airflow plugin.

.. code-block:: python

    from airflow.lineage.hook_lineage import HookLineageReader
    from airflow.plugins_manager import AirflowPlugin


    class CustomHookLineageReader(HookLineageReader):
        def get_inputs(self):
            return self.lineage_collector.collected_assets.inputs


    class HookLineageCollectionPlugin(AirflowPlugin):
        name = "HookLineageCollectionPlugin"
        hook_lineage_readers = [CustomHookLineageReader]

If no ``HookLineageReader`` is registered within Airflow, a default ``NoOpCollector`` is used instead.
This collector does not create AIP-60 compliant assets or collect lineage information.


Lineage Backend
---------------

It's possible to push the lineage metrics to a custom backend by providing an instance of a LineageBackend in the config:

.. code-block:: ini

  [lineage]
  backend = my.lineage.CustomBackend

The backend should inherit from ``airflow.lineage.LineageBackend``.

.. code-block:: python

  from airflow.lineage.backend import LineageBackend


  class CustomBackend(LineageBackend):
      def send_lineage(self, operator, inlets=None, outlets=None, context=None):
          ...
          # Send the info to some external service
