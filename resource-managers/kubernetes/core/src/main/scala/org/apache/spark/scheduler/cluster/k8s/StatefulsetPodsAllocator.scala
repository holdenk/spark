/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package org.apache.spark.scheduler.cluster.k8s

import java.util.concurrent.TimeUnit

import scala.collection.JavaConverters._
import scala.collection.mutable

import io.fabric8.kubernetes.api.model.{PodSpec, PodSpecBuilder, PodTemplateSpec}
import io.fabric8.kubernetes.client.KubernetesClient

import org.apache.spark.{SecurityManager, SparkConf, SparkException}
import org.apache.spark.deploy.k8s.Config._
import org.apache.spark.deploy.k8s.Constants._
import org.apache.spark.deploy.k8s.KubernetesConf
import org.apache.spark.deploy.k8s.KubernetesUtils.addOwnerReference
import org.apache.spark.internal.Logging
import org.apache.spark.util.{Clock, Utils}

private[spark] class StatefulsetPodsAllocator(
    conf: SparkConf,
    executorBuilder: KubernetesExecutorBuilder,
    kubernetesClient: KubernetesClient,
    snapshotsStore: ExecutorPodsSnapshotsStore,
    clock: Clock) extends AbstractPodsAllocator() with Logging {

  private val namespace = conf.get(KUBERNETES_NAMESPACE)

  private val kubernetesDriverPodName = conf
    .get(KUBERNETES_DRIVER_POD_NAME)

  val driverPod = kubernetesDriverPodName
    .map(name => Option(kubernetesClient.pods()
      .withName(name)
      .get())
      .getOrElse(throw new SparkException(
        s"No pod was found named $name in the cluster in the " +
          s"namespace $namespace (this was supposed to be the driver pod.).")))

  var appId = ""

  def start(applicationId: String): Unit = {
    appId = applicationId
    driverPod.foreach { pod =>
      // Wait until the driver pod is ready before starting executors, as the headless service won't
      // be resolvable by DNS until the driver pod is ready.
      Utils.tryLogNonFatalError {
        kubernetesClient
          .pods()
          .withName(pod.getMetadata.getName)
          .waitUntilReady(100000, TimeUnit.SECONDS)
      }
    }
  }

  def setTotalExpectedExecutors(total: Int): Unit = {
    setTargetExecutorsReplicaset(total, appId)
  }

  // For now just track the sets created, in the future maybe track requested value too.
  val setsCreated = new mutable.HashSet[String]()

  private def setName(applicationId: String): String = {
    s"spark-s-${applicationId}"
  }

  private def setTargetExecutorsReplicaset(
      expected: Int,
      applicationId: String): Unit = {
    if (setsCreated.contains(applicationId)) {
      val statefulset = kubernetesClient.apps().statefulSets().withName(
        setName(applicationId))
      statefulset.scale(expected, false /* wait */)
    } else {
      // We need to make the new replicaset which is going to involve building
      // a pod.
      val executorConf = KubernetesConf.createExecutorConf(
        conf,
        "EXECID",// template exec IDs
        applicationId,
        driverPod)
      val resolvedExecutorSpec = executorBuilder.buildFromFeatures(executorConf)
      val executorPod = resolvedExecutorSpec.pod
      val podSpecBuilder = executorPod.getSpec() match {
        case null => new PodSpecBuilder()
        case s => new PodSpecBuilder(s)
      }
      val podWithAttachedContainer: PodSpec = podSpecBuilder
        .addToContainers(resolvedExecutorSpec.container)
        .build()

      val meta = executorPod.getMetadata()

      // Create a pod template spec from the pod.
      val podTemplateSpec = new PodTemplateSpec(meta, podWithAttachedContainer)

      val statefulSet = new io.fabric8.kubernetes.api.model.apps.StatefulSetBuilder()
        .withNewMetadata()
          .withName(setName(applicationId))
          .withNamespace(conf.get(KUBERNETES_NAMESPACE))
        .endMetadata()
        .withNewSpec()
          .withPodManagementPolicy("Parallel")
          .withReplicas(expected)
          .withNewSelector()
            .addToMatchLabels(SPARK_APP_ID_LABEL, applicationId)
            .addToMatchLabels(SPARK_ROLE_LABEL, SPARK_POD_EXECUTOR_ROLE)
          .endSelector()
          .withTemplate(podTemplateSpec)
        .endSpec()
        .build()

      addOwnerReference(driverPod.get, Seq(statefulSet))
      kubernetesClient.apps().statefulSets().create(statefulSet)
      setsCreated += (applicationId)
    }
  }

  override def stop(applicationId: String): Unit = {
    // Cleanup the statefulsets when we stop
    Utils.tryLogNonFatalError {
      kubernetesClient.apps().statefulSets().withName(setName(applicationId)).delete()
    }
  }
}
