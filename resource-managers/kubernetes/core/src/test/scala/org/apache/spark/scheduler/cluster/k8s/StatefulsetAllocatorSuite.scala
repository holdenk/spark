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

import io.fabric8.kubernetes.api.model._
import io.fabric8.kubernetes.api.model.apps.StatefulSet
import io.fabric8.kubernetes.client.KubernetesClient
import io.fabric8.kubernetes.client.dsl._
import org.mockito.{ArgumentCaptor, ArgumentMatcher, Matchers, Mock, MockitoAnnotations}
import org.mockito.Matchers.{any, eq => meq}
import org.mockito.Mockito.{never, times, verify, when}
import org.mockito.invocation.InvocationOnMock
import org.mockito.stubbing.Answer
import org.scalatest.BeforeAndAfter

import org.apache.spark.{SecurityManager, SparkConf, SparkFunSuite}
import org.apache.spark.deploy.k8s._
import org.apache.spark.deploy.k8s.Config._
import org.apache.spark.deploy.k8s.Constants._
import org.apache.spark.deploy.k8s.Fabric8Aliases._
import org.apache.spark.internal.config.DYN_ALLOCATION_EXECUTOR_IDLE_TIMEOUT
import org.apache.spark.scheduler.cluster.k8s.ExecutorLifecycleTestUtils._

class StatefulSetAllocatorSuite extends SparkFunSuite with BeforeAndAfter {

  private val driverPodName = "driver"

  private val driverPod = new PodBuilder()
    .withNewMetadata()
      .withName(driverPodName)
      .addToLabels(SPARK_APP_ID_LABEL, TEST_SPARK_APP_ID)
      .addToLabels(SPARK_ROLE_LABEL, SPARK_POD_DRIVER_ROLE)
      .withUid("driver-pod-uid")
      .endMetadata()
    .build()

  private val conf = new SparkConf()
    .set(KUBERNETES_DRIVER_POD_NAME, driverPodName)
    .set(DYN_ALLOCATION_EXECUTOR_IDLE_TIMEOUT.key, "10s")


  @Mock
  private var kubernetesClient: KubernetesClient = _

  @Mock
  private var appOperations: AppsAPIGroupDSL = _

  @Mock
  private var statefulSetOperations: MixedOperation[
    apps.StatefulSet, apps.StatefulSetList, apps.DoneableStatefulSet,
    RollableScalableResource[apps.StatefulSet, apps.DoneableStatefulSet]] = _

  @Mock
  private var editableSet: RollableScalableResource[apps.StatefulSet, apps.DoneableStatefulSet] = _

  @Mock
  private var podOperations: PODS = _


  @Mock
  private var driverPodOperations: PodResource[Pod, DoneablePod] = _

  private var podsAllocatorUnderTest: StatefulsetPodsAllocator = _

  private var snapshotsStore: DeterministicExecutorPodsSnapshotsStore = _

  @Mock
  private var executorBuilder: KubernetesExecutorBuilder = _

  @Mock
  private var schedulerBackend: KubernetesClusterSchedulerBackend = _

  val appId = "testapp"

  private def executorPodAnswer(): Answer[SparkPod] = {
    new Answer[SparkPod] {
      override def answer(invocation: InvocationOnMock): SparkPod = {
        val k8sConf = invocation.getArgumentAt(
          0, classOf[KubernetesConf[KubernetesExecutorSpecificConf]])
        executorPodWithId(k8sConf.roleSpecificConf.executorId.toInt)
      }
    }
  }

  before {
    MockitoAnnotations.initMocks(this)
    when(kubernetesClient.pods()).thenReturn(podOperations)
    when(kubernetesClient.apps()).thenReturn(appOperations)
    when(appOperations.statefulSets()).thenReturn(statefulSetOperations)
    when(statefulSetOperations.withName(any())).thenReturn(editableSet)
    when(podOperations.withName(driverPodName)).thenReturn(driverPodOperations)
    when(driverPodOperations.get).thenReturn(driverPod)
    when(driverPodOperations.waitUntilReady(any(), any())).thenReturn(driverPod)
    when(executorBuilder.buildFromFeatures(kubernetesConfWithCorrectFields())).thenAnswer(
      executorPodAnswer())
    snapshotsStore = new DeterministicExecutorPodsSnapshotsStore()
    podsAllocatorUnderTest = new StatefulsetPodsAllocator(
      conf, executorBuilder, kubernetesClient, snapshotsStore, null)
    when(schedulerBackend.getExecutorIds).thenReturn(Seq.empty)
    podsAllocatorUnderTest.start(TEST_SPARK_APP_ID)
  }

  test("Validate initial statefulSet creation & cleanup with two resource profiles") {
    podsAllocatorUnderTest.start(appId)
    podsAllocatorUnderTest.setTotalExpectedExecutors(10)
    val captor = ArgumentCaptor.forClass(classOf[StatefulSet])
    verify(statefulSetOperations, times(1)).create(any())
    podsAllocatorUnderTest.stop(appId)
    verify(editableSet, times(1)).delete()
  }

  test("Validate statefulSet scale up") {
    podsAllocatorUnderTest.start(appId)
    podsAllocatorUnderTest.setTotalExpectedExecutors(10)

    val captor = ArgumentCaptor.forClass(classOf[StatefulSet])
    verify(statefulSetOperations, times(1)).create(captor.capture())
    val set = captor.getValue()
    val setName = set.getMetadata().getName()
    val namespace = set.getMetadata().getNamespace()
    assert(namespace === "default")
    val spec = set.getSpec()
    assert(spec.getReplicas() === 10)
    assert(spec.getPodManagementPolicy() === "Parallel")
    verify(podOperations, never()).create(any())
    podsAllocatorUnderTest.setTotalExpectedExecutors(20)
    verify(editableSet, times(1)).scale(any(), any())
  }

  private def kubernetesConfWithCorrectFields(): KubernetesConf[KubernetesExecutorSpecificConf] =
    Matchers.argThat(new ArgumentMatcher[KubernetesConf[KubernetesExecutorSpecificConf]] {
      override def matches(argument: scala.Any): Boolean = {
        if (!argument.isInstanceOf[KubernetesConf[KubernetesExecutorSpecificConf]]) {
          false
        } else {
          val k8sConf = argument.asInstanceOf[KubernetesConf[KubernetesExecutorSpecificConf]]
          val executorSpecificConf = k8sConf.roleSpecificConf
          val expectedK8sConf = KubernetesConf.createExecutorConf(
            conf,
            executorSpecificConf.executorId,
            TEST_SPARK_APP_ID,
            Some(driverPod))

          // Set prefixes to a common string since KUBERNETES_EXECUTOR_POD_NAME_PREFIX
          // has not be set for the tests and thus KubernetesConf will use a random
          // string for the prefix, based on the app name, and this comparison here will fail.
          val k8sConfCopy = k8sConf
            .copy(appResourceNamePrefix = "")
            .copy(sparkConf = conf)
          val expectedK8sConfCopy = expectedK8sConf
            .copy(appResourceNamePrefix = "")
            .copy(sparkConf = conf)

            k8sConf.sparkConf.getAll.toMap == conf.getAll.toMap &&
            // Since KubernetesConf.createExecutorConf clones the SparkConf object, force
            // deep equality comparison for the SparkConf object and use object equality
            // comparison on all other fields.
            k8sConfCopy == expectedK8sConfCopy
        }
      }
    })
}
